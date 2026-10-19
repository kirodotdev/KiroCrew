"""The text transforms a Discord turn applies to model output before it is shown.

The trailing ``[OPTIONS:]`` and steering-marker strips (an unclosed marker still
streaming in included), the delivered form a candidate cut is graded through, the check
that a table card buries no local image reference, Discord subtext, the steer-chip
neutralizer and the platform-cap slicer every seal bounds its payloads with. None of
them redacts: the display-form redaction pass stays with the sends in
:mod:`kiro_crew.discord.renderer`. New text transforms of this kind go here;
:mod:`kiro_crew.discord.renderer` imports these and stays their import path and patch
surface.
"""

from __future__ import annotations

import re

from kiro_crew.discord.client import DISCORD_MAX_TEXT
from kiro_crew.messaging.outbound_files import protected_ref_spans
from kiro_crew.messaging.renderer import chunk_text, split_options_trailer

# kiro-cli's inline "[STEERING steer-<id>: …]" steer-ack marker (see the
# Telegram renderer for the full rationale — Discord likewise has no parser).
#
# The frame is recognised by its GRAMMAR, and that is where the summary is
# allowed to contain newlines: ``messaging.driver._STEER_MARKER_RE`` reads the
# same frame with ``re.DOTALL``, ``constants._STEERING_TAIL_PREFIX_RE`` closes
# that grammar's prefix with ``re.DOTALL`` too, and the dashboard's own parser
# (``website/src/app-sdk/protocol/steering.ts``) spells the summary
# ``[\s\S]*?``. kiro-cli's rephrase is free to wrap, so a class that stopped at
# the first line end left a real marker unrecognised. ``]`` is the terminator
# this grammar actually has.
#
# Requiring ``steer-<id>`` rather than a bare ``[STEERING`` is the ruling
# ``messaging.driver`` already applies: opening with the sentinel is not being a
# marker, so prose that merely mentions it stays visible now that the class no
# longer stops at a line end. The id class matches driver's, and is the SAME in
# both patterns, because the summary is matched at the offset the marker pattern
# chose -- a narrower id class there would silently drop the summary.
_STEER_MARKER_RE = re.compile(r"\[STEERING\s+steer-[0-9a-f-]+(?:\s*:[^\]]*)?\]", re.IGNORECASE)
_STEER_SUMMARY_RE = re.compile(r"\[STEERING\s+steer-[0-9a-f-]+\s*:\s*([^\]]*)\]", re.IGNORECASE)


def _extract_options(text: str) -> tuple[str, list[str]]:
    """Split text into ``(body, options)``, holding back a streamed partial.

    ``hide_partial=True`` because this renderer STREAMS: a still-arriving
    ``[OPTIONS…`` fragment really may be a marker mid-flight, and the next frame
    re-renders from the full buffer, so hiding it costs nothing permanent.
    """
    return split_options_trailer(text, hide_partial=True)


def _strip_steering(text: str) -> str:
    """Remove kiro-cli's inline ``[STEERING …]`` steer-ack marker from output,
    including an UNCLOSED trailing fragment still streaming in (see the
    Telegram renderer for the show-then-vanish rationale)."""
    cleaned = _STEER_MARKER_RE.sub("", text)
    cleaned = re.sub(r"\[STEERING\b[^\]\r\n]*$", "", cleaned)  # unclosed, streaming
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _delivered_form(source: str) -> str:
    """*source* as the reader will actually see it, for grading a cut.

    ``safe_split_offset`` grades both sides of a candidate boundary through this,
    and its contract names surrounding whitespace as one of the things a renderer
    removes on the way out. Trimming matters more than it looks: a cut placed just
    after a trailing space leaves each raw half safe while the delivered halves sit
    flush together, and Discord shows the reader the trimmed form. Modelling MORE
    trimming than a path happens to perform only makes the grade stricter, which is
    the safe direction; modelling less is what lets a boundary through.
    """
    return _strip_steering(source).strip()


def _transform_buries_refs(canonical: str, presented: str) -> bool:
    """True when a presentation transform hides a local ref extraction needs.

    Upload extraction reads the DELIVERED text, so a transform that fences an
    in-cell image leaves no extractable span and the raw filesystem path would
    ship as literal content.

    Compares COUNTS rather than emptiness because loss is per-ref: a message
    carrying a prose image beside a grid-form table keeps the prose span while
    losing the in-cell one, which would upload the first picture and expose the
    second one's path. Any drop is a loss.
    """
    return len(protected_ref_spans(presented)) < len(protected_ref_spans(canonical))


def _as_subtext(text: str) -> str:
    """Render *text* as Discord subtext.

    The ``-# `` marker applies to ONE line, so a multi-line note needs it on
    each; a blank line would end the block, so blank lines are dropped rather
    than emitted as a bare marker.
    """
    lines = (line.strip() for line in text.splitlines())
    return "\n".join(f"-# {line}" for line in lines if line)


def _neutralize_md(raw: str) -> str:
    """Collapse whitespace, cap length, and strip Markdown control chars from a
    steer's text so the chip renders literally (inside a blockquote) and can't
    perturb surrounding formatting."""
    t = " ".join((raw or "").split())[:120]
    return re.sub(r"[*_`\[\]()]", "", t)


def _fit_platform_cap(text: str, limit: int = DISCORD_MAX_TEXT) -> list[str]:
    """Slice *text* into payloads Discord's message API will accept whole.

    ``split_markdown_safe`` budgets every chunk against :meth:`_limit`, with one
    documented exception: a logical line that admits no cut clean on both sides
    is placed WHOLE rather than cut into a fence delimiter its source never
    contained, and the chunk carries its fence scaffolding — the reopener line
    plus the newline and synthetic closer — on top of the limit. The 100
    characters :meth:`_limit` holds back absorb ordinary scaffolding, but an
    opener line long enough (a several-hundred-backtick run, a huge info string)
    still pushes such a chunk past Discord's hard cap. ``client.send_message``
    truncates to that cap, which drops the tail INCLUDING the synthetic closer,
    so the user reads an unterminated code block missing content and gets no
    signal that anything was lost.

    Blind fixed-width slicing is the right last resort in exactly that regime:
    it keeps every authored character at the price of a boundary Markdown may
    render badly, where truncation keeps neither. Nothing here re-derives fence
    grammar — the splitter owns that, and this only bounds what reaches the API.
    """
    return chunk_text(text, limit) or [text]

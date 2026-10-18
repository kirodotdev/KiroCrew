"""How much room a Telegram segment takes, and what of it the reader may see.

The text helpers :class:`~kiro_crew.telegram.renderer.TelegramRenderer` cleans and
measures a segment with: the display-form redaction (``_display_safe``), Telegram's
UTF-16 length, cut and chunking, the trailing ``[OPTIONS:]`` and steering-marker strips,
the horizontal-rule strip, and the shrunk budget a re-split falls back to when a
rendered form outgrows its limit. New helpers of that kind go here.
:mod:`kiro_crew.telegram.renderer` imports these and stays their import path and patch
surface.
"""

from __future__ import annotations

import re

from kiro_crew.messaging.display_safety import TELEGRAM_FALLBACK_FENCE, redact_for_display
from kiro_crew.messaging.renderer import _default_redactor, split_options_trailer


def _display_safe(text: str) -> str:
    """Redact against the form Telegram RENDERS, not the bytes we send.

    The byte-level pass in ``TurnDriver`` runs before this renderer introduces any
    markup, and it cannot see a credential that markup will reassemble:
    ``redact_credentials("AKIA**IOSFODNN7EXAMPLE**")`` matches nothing because the
    ``**`` sits inside the key, and then ``_md_to_telegram_html`` emits
    ``AKIA<b>IOSFODNN7EXAMPLE</b>`` — which Telegram displays as an intact access
    key. ``[AKIA](https://x)IOSFODNN7EXAMPLE`` is the same hazard through a link,
    and a zero-width character between two halves is the same hazard with no
    markup at all. ``redact_for_display`` canonicalizes to the rendered form and
    scans BOTH that and the literal, which is why it exists and why Slack and
    Discord both run it at their own render boundaries.

    Applied at the two sinks, BEFORE any tags are introduced: the live plaintext
    frame and the seal. A redaction can make the text LONGER than the segment
    budget that sized it; that is accepted, and the seal re-measures and re-splits
    against the render cap. Losing formatting to keep a rendered secret redacted
    is the documented trade.

    Runs the SHARED ``_default_redactor`` (exfil URLs then credentials), the same
    pair ``TurnDriver`` streams provider text through, so a display sink cannot end
    up scanning for less than the stream did.
    """
    safe, _ = redact_for_display(text or "", _default_redactor)
    return safe


def _utf16_len(text: str) -> int:
    """Length of *text* in UTF-16 code units — the unit of Telegram's caps.

    Python counts code points; the Bot API counts UTF-16 units, so every
    astral character (emoji, most notably) costs 2 against Telegram's 4096
    while costing 1 against ``len``. The entity machinery in
    ``telegram/client.py`` already measures in these units; message budgets
    here measure the same way.
    """
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _utf16_cut(text: str, limit: int) -> int:
    """Largest CODE-POINT index whose prefix fits ``limit`` UTF-16 units.

    Returning a code-point index means ``str`` slicing can never bisect a
    surrogate pair: an astral character either fits whole or is excluded
    whole. The floor of 2 is load-bearing — at ``limit=1`` a leading astral
    character (2 units) would cut at index 0, and a caller consuming the
    text chunk by chunk would stop making progress.
    """
    budget = max(2, limit)
    units = 0
    for index, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > budget:
            return index
    return len(text)


def _utf16_chunks(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` UTF-16 units each.

    Prefers newline boundaries so a line (one restored image reference, in
    the recovery caller) stays whole within one message; a single line
    larger than the whole budget is hard-cut at a code-point boundary
    rather than dropped. Boundary newlines are consumed by the split.
    """
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if _utf16_len(candidate) <= max(2, limit):
            current = candidate
            continue
        if current:
            chunks.append(current)
        while _utf16_len(line) > max(2, limit):
            cut = _utf16_cut(line, limit)
            chunks.append(line[:cut])
            line = line[cut:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def _extract_options(text: str) -> tuple[str, list[str]]:
    """Split text into ``(body, options)``, holding back a streamed partial.

    ``hide_partial=True`` because this renderer STREAMS: a still-arriving
    ``[OPTIONS…`` fragment really may be a marker mid-flight, and the next frame
    re-renders from the full buffer, so hiding it costs nothing permanent.
    """
    return split_options_trailer(text, hide_partial=True)


# kiro-cli emits an inline "[STEERING steer-<id>: …]" ack marker when it folds a
# mid-turn steer at a boundary. The dashboard parses it into a chip; Telegram has
# no parser, so strip it — the user's own steer message already shows the
# instruction (and gets a steer-ack reaction), so the raw inline marker is just
# redundant noise in the bubble.
#
# The frame is recognised by its GRAMMAR, and opening with the sentinel is not
# being a marker: ``messaging.driver._STEER_MARKER_RE`` requires ``steer-<id>``,
# and so does the dashboard's own parser
# (``website/src/app-sdk/protocol/steering.ts``). A bare ``[STEERING`` class
# matched ordinary prose that merely mentions the sentinel and deleted it from
# the delivered message — and because ``[^\]]*`` does not stop at a line end, it
# ran on to whatever ``]`` came next, taking the text in between with it.
#
# The id class matches driver's ``[0-9a-f-]+`` and is the SAME in both patterns,
# because the summary is matched at the offset the marker pattern chose — a
# narrower class there silently drops the summary. That is what the dash did:
# the marker matched a dashed id through its old catch-all, the summary pattern
# did not, and the chip lost the one piece of new information it carries.
_STEER_MARKER_RE = re.compile(r"\[STEERING\s+steer-[0-9a-f-]+(?:\s*:[^\]]*)?\]", re.IGNORECASE)
# Same marker, capturing the ack SUMMARY kiro-cli embeds after "steer-<id>:".
# The dashboard renders this summary as its "Steered — …" chip; we prefer it
# for the Telegram chip too (the user's own words are already on screen as
# their message — the summary is the only NEW information).
_STEER_SUMMARY_RE = re.compile(r"\[STEERING\s+steer-[0-9a-f-]+\s*:\s*([^\]]*)\]", re.IGNORECASE)


def _strip_steering(text: str) -> str:
    """Remove kiro-cli's inline ``[STEERING …]`` steer-ack marker from output.

    Also strips an UNCLOSED trailing ``[STEERING …`` (still streaming, no closing
    ``]`` yet): otherwise the marker's long rephrase streams into the live draft
    and then vanishes when ``on_done`` finally strips the completed marker — a
    jarring show-then-vanish. Stripping the partial marker keeps the draft in
    sync with the final message.
    """
    cleaned = _STEER_MARKER_RE.sub("", text)  # complete markers anywhere
    cleaned = re.sub(r"\[STEERING\b[^\]]*$", "", cleaned)  # unclosed, still streaming
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)  # collapse gaps left behind
    return cleaned.strip()


def _neutralize_md(raw: str) -> str:
    """Collapse whitespace, cap length, and strip Markdown control chars from a
    steer's text so the chip renders literally (inside a blockquote) and can't
    perturb surrounding formatting."""
    t = " ".join((raw or "").split())[:120]
    return re.sub(r"[*_`\[\]()]", "", t)


def _strip_hr(text: str) -> str:
    """Drop Markdown horizontal rules (``---`` / ``***`` / ``___`` on their own
    line) — they render as literal dashes on Telegram and just add noise.
    Fenced code blocks are stashed first so a standalone ``---`` INSIDE a code
    block (e.g. a YAML document separator) is never touched. An unclosed fence
    mid-stream isn't stashed, but live frames are throttled previews — the
    final seal sees the closed fence and preserves its content."""
    stash: list[str] = []

    def _keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00H{len(stash) - 1}\x00"

    text = TELEGRAM_FALLBACK_FENCE.sub(lambda m: _keep(m.group(0)), text)
    # Max 3 leading spaces (markdown HR rule) — a 4-space-indented "---" is
    # indented CODE (e.g. a YAML separator) and must survive.
    out = re.sub(r"(?m)^[ ]{0,3}([-*_])\1{2,}[ \t]*$", "", text)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"\x00H(\d+)\x00", lambda m: stash[int(m.group(1))], out)
    return out.strip()


#: Minimum source-chunk budget when shrinking to fit the rendered cap. Below
#: this, shrinking stops making progress; the caller's tag-safe truncation and
#: plaintext fallback are the backstop for genuinely indivisible content.
_MIN_SPLIT_LIMIT = 400

#: How much one shrink round cuts at minimum, BEFORE the ``_MIN_SPLIT_LIMIT``
#: clamp. The proportional step alone cuts 5% of the current budget, which for a
#: small budget is a few tens of characters, so this is what bounds the number of
#: re-split rounds; above roughly 20x it the 5% cut is the larger of the two and
#: this never binds. Near the floor the clamp wins and the real cut is smaller.
_SHRINK_STEP = 128


def _shrunk_limit(current: int, rendered_cap: int, worst: int) -> int:
    """The next source budget to try after a chunk rendered past ``rendered_cap``.

    Shrinks in proportion to the observed inflation (``rendered_cap / worst``)
    with a 5% margin, taking at least ``_SHRINK_STEP`` unless the floor is
    nearer than that, and drops straight to ``_MIN_SPLIT_LIMIT`` when the
    proportional answer would not shrink at all.

    **Callers must hold ``current > _MIN_SPLIT_LIMIT`` and
    ``worst > rendered_cap > 0``.** Under those the result is strictly below
    *current* and never below the floor, which is what makes a shrink loop
    terminate; at or below the floor there is nowhere left to shrink to and the
    answer is the floor itself, so a caller that skips the guard would spin.
    Both call sites guard it before they get here.
    """
    scaled = int(current * (rendered_cap / worst) * 0.95)
    nxt = max(_MIN_SPLIT_LIMIT, min(scaled, current - _SHRINK_STEP))
    return nxt if nxt < current else _MIN_SPLIT_LIMIT


def _delivered_form(source: str) -> str:
    """What a seal actually SENDS for ``source``, as a credential scan must see it.

    Mirrors ``_segment_text`` followed by ``_seal_current``'s own ``strip``. A cut is
    graded before the seal runs, so grading the raw chunk grades text the reader
    never gets: the facing edges of two chunks can be whitespace, a steering marker
    or a horizontal rule, all of which disappear here -- and once they do, the two
    messages sit flush against each other on screen.
    """
    return _strip_hr(_strip_steering(source)).strip()


#: Markup that makes ``_md_to_telegram_html`` EMIT TAGS, and therefore grow the
#: text by an amount no per-character arithmetic can bound: a line-leading ``>``
#: (blockquote) or ``#`` (heading), or any ``*``/``_``/backtick/``[`` (bold,
#: italic, code span, link). Cheap single-pass scan; used only as a gate.
_MARKUP_HINT_RE = re.compile(r"(?m)^[ \t]{0,3}[>#]|[*_`\[]")


def _may_exceed_rendered(source: str, cap: int) -> bool:
    """Conservative "could this render past ``cap``?" gate.

    A ``False`` MUST mean "provably fits" -- it is the only case the caller skips
    the authoritative ``_rendered_len`` check for, so under-estimating ships
    oversize HTML. Over-returning ``True`` is always safe: it costs one real
    render on a buffer that is nowhere near the cap.

    Per-construct growth constants were tried and rejected as whack-a-mole --
    blockquote, heading, bold/italic and code spans each need their own term,
    the measured inflation reaches 3.75x (``"`x` " * 700``: 2800 source chars ->
    10500 rendered), and any new converter rule silently reintroduces
    unsoundness. Instead trust the cheap arithmetic ONLY for prose containing no
    tag-producing markup at all, and measure everything else for real.
    """
    if len(source) >= cap:
        return True
    if _MARKUP_HINT_RE.search(source):
        return True  # tags will be emitted -- refuse to guess, measure instead
    # Tag-free prose: the only growth left is ``html.escape``, which adds at most
    # 5 chars per escape-worthy character (``'`` -> ``&#x27;``).
    escapes = sum(source.count(c) for c in ("&", "<", ">", '"', "'"))
    return len(source) + 5 * escapes >= cap

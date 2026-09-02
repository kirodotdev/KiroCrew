"""Classify an assembled agent prompt into the blocks KiroCrew injected.

The assembly in :mod:`kiro_crew.context` appends ~30 separately-sourced strings
into one prompt. Attributing size back to those sources by instrumenting every
append site would put a counter next to each one and drift the moment a new
block is added without its counter. Instead this module classifies the FINAL
string by the bracket markers the assembly already emits — the markers are a
contract with the model, so they are stable, and reading the output means the
attribution cannot disagree with what was actually sent.

Anything not matched lands in ``unclassified``, so a new (or renamed) block
shows up as a visible bucket instead of being silently folded into a neighbour.

Sizes are characters, not tokens. Characters are exact, free, and deterministic;
the only tokenizer available here is OpenAI's BPE, which would add a systematic
unknown error against a Claude backend. For comparing one turn against another —
the point of the breakdown — an exact unit beats an approximate one.
"""

from __future__ import annotations

import re
from typing import Final

# Label for the user's own text, and for the remainder we could not attribute.
USER_LABEL: Final = "your_message"
UNCLASSIFIED_LABEL: Final = "unclassified"

# Ordered (label, opening-marker) pairs. A block owns the span from its marker
# up to the next marker's start, mirroring how the assembly concatenates them.
# Labels are stable identifiers — the UI maps them to display names, so
# renaming one here is a breaking change for stored rows.
_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("critical_rules", r"\[CRITICAL RULES"),
    ("agent_instructions", r"\[AGENT SYSTEM PROMPT\]"),
    ("session_wrapper", r"\[SESSION CONTEXT"),
    ("date", r"\[CURRENT DATE\]"),
    ("agent_identity", r"\[CURRENT AGENT\]"),
    ("surface", r"\[RUNTIME\]"),
    ("workspace_identity", r"\[WORKSPACE IDENTITY\]"),
    ("docs_pointer", r"\[DOCUMENTATION\]"),
    ("memory", r"\[Memory\b"),
    ("semantic_memory", r"\[Semantic Memory"),
    ("skill_index", r"\[Skills:\]"),
    ("lessons", r"\[Learned corrections"),
    ("episodic_memory", r"\[Episodic Memory"),
    ("loaded_skill", r"\[Skill: "),
    ("skill_hint", r"\[Relevant skills for this message\]"),
    ("channel_context", r"\[SLACK THREAD CONTEXT"),
    ("conversation_replay", r"\[CONVERSATION HISTORY"),
    ("history_prefix", r"\[Previous chat history for this tab"),
    ("hook_context", r"\[Hook context[:\]]"),
    ("system_notice", r"\[System: "),
    ("theme_persona", r"\[THEME PERSONA\]"),
    ("working_folder", r"\[PROJECT\]"),
    ("folder_path", r"\[FOLDER\]"),
    ("resource_advisory", r"\[RESOURCES\]"),
    ("user_display", r"\[CURRENT USER\]"),
    ("user_profile", r"\[USER PROFILE\]"),
    ("ui_language", r"\[UI LANGUAGE\]"),
    ("channel_persona", r"\[CHANNEL\]"),
    ("incognito", r"\[INCOGNITO SESSION\]"),
    ("temporary_session", r"\[TEMPORARY SESSION\]"),
    ("cancelled_turn", r"\[PREVIOUS TURN WAS CANCELLED"),
    ("request_header", r"\[CURRENT USER REQUEST"),
)

_COMPILED: Final = tuple((label, re.compile(pat)) for label, pat in _MARKERS)

# Closing markers, by label. A block that has one owns only up to its OWN
# closer; the characters between that closer and the next opening marker belong
# to no block anybody named and are reported as ``unclassified``.
#
# Without this, a block's span ran all the way to the next marker it could find,
# which silently turned "unattributed" into "MIS-attributed" — and the panel has
# no way to tell a genuinely large block from a small one that swallowed its
# neighbours. It is not a rare edge either: whichever blocks are conditional
# (workspace identity and the docs pointer are skipped for a custom agent, the
# memory family for a session sealed from the user's memory) are exactly the
# ones whose absence lets an earlier block absorb everything downstream of it.
# Measured on one real session: a ~470-char profile block was reported as 8,116.
#
# Only closers that the assembly actually emits are listed, so adding one is a
# data change here rather than an edit to the scanner. A single-line block
# (``[CURRENT DATE]``, ``[RUNTIME]``, …) has no closer and keeps the old
# next-marker behaviour, which is correct for it: there is nothing in between.
#
# Nesting needs no special case. ``[SESSION CONTEXT`` wraps the memory family and
# closes long after it, but the search below is bounded by the next opening
# marker, so a wrapper whose nested blocks start first simply never finds its own
# closer in range and ends where it always did.
_CLOSERS: Final[dict[str, re.Pattern[str]]] = {
    label: re.compile(pat)
    for label, pat in (
        ("critical_rules", r"\[END CRITICAL RULES\]"),
        ("agent_instructions", r"\[END AGENT SYSTEM PROMPT\]"),
        ("session_wrapper", r"\[END OF SESSION CONTEXT\]"),
        ("workspace_identity", r"\[End of workspace identity\]"),
        ("docs_pointer", r"\[END DOCUMENTATION\]"),
        ("memory", r"\[End of memory\]"),
        ("semantic_memory", r"\[End of semantic memory\]"),
        ("skill_index", r"\[End of skills\]"),
        ("lessons", r"\[End of learned corrections\]"),
        ("episodic_memory", r"\[End of episodic memory\]"),
        # Escaped `]` matters: `[End of skill]` is a prefix of `[End of skills]`,
        # and an unanchored pattern would let one loaded skill claim the whole
        # skills index that follows it.
        ("loaded_skill", r"\[End of skill\]"),
        ("skill_hint", r"\[End of relevant skills\]"),
        ("channel_context", r"\[END SLACK THREAD CONTEXT\]"),
        ("conversation_replay", r"\[END CONVERSATION HISTORY\]"),
        # Two spellings reach this opener: `context.py` emits
        # `[End of hook context]` and `chat_runner.py` emits `[End hook
        # context]`. Both are real emit sites, so one alternation covers them
        # rather than picking whichever the assembly happened to use last.
        ("hook_context", r"\[End (?:of )?hook context\]"),
        ("history_prefix", r"\[End of history\]"),
        ("theme_persona", r"\[END THEME PERSONA\]"),
        ("user_profile", r"\[End of user profile\]"),
        ("ui_language", r"\[End of UI language\]"),
        ("cancelled_turn", r"\[END PREVIOUS TURN\]"),
    )
}

# The reply-format / tool-contract paragraphs the assembly appends after the
# user's text. They open with a parenthesis at the start of a line and are the
# only trailing blocks, so one anchored pattern covers all of them.
_TRAILING_CONTRACTS: Final = re.compile(r"\n\n\((?:If |When )", re.MULTILINE)

# Blocks that ride along on EVERY turn rather than once at session start.
# Grouped under one display label because individually they are a few hundred
# characters each and the useful fact about them is that they repeat.
EVERY_TURN_LABELS: Final[frozenset[str]] = frozenset(
    {"surface", "working_folder", "request_header", "reply_format_rules", "user_display"}
)

REPLY_FORMAT_LABEL: Final = "reply_format_rules"

PHASE_SESSION_START: Final = "session_start"
PHASE_PER_TURN: Final = "per_turn"


def attributable_user_chars(original_len: int, *, prompt_expanded: bool) -> int:
    """Chars of the FINAL message that are the user's own typed text.

    Input expansions mutate the message before it is classified, so the raw
    ``len(message)`` at classification time over-credits the user:

    * ``@prompt`` REPLACES the message with SOP/prompt content — injected
      instruction, not user typing — so none of it is the user's (return 0).
    * A ``/plain``-style QUICK PROMPT is the same class of replacement: the token
      the user typed is gone from the turn and what remains is generated
      instruction, so none of it is theirs either. ``build_message`` reports an
      empty user span for that turn, and the caller passes ``prompt_expanded``
      for it, so this fallback agrees with the authoritative span.
    * ``$skill`` APPENDS skill bodies after the original text, so the original
      typed length still marks the user's span; the appended ``[Skill: ]``
      blocks classify by their own markers in :func:`split_blocks`.
    * With neither expansion the message is the user's text unchanged.

    ``original_len`` is ``len(message)`` captured BEFORE any expansion.
    """
    return 0 if prompt_expanded else original_len


def split_blocks(
    prompt: str,
    *,
    user_chars: int = 0,
    user_offset: int = 0,
    user_span: tuple[int, int] | None = None,
) -> dict[str, int]:
    """Attribute ``prompt``'s characters to the block that produced them.

    ``user_span`` is the AUTHORITATIVE ``(start, end)`` of the user's own text in
    ``prompt``, as reported by ``build_message`` (which is the only code that sees
    the turn after a rewriting hook, marker neutralization and the multibyte fold
    have all been applied). Pass it whenever it is available: the ``user_chars`` /
    ``user_offset`` pair below reconstructs the same span from pre-transform
    lengths, which drifts whenever the assembly grows another transform.

    ``user_chars`` is the length of the user's own text as appended (the caller
    knows it exactly; parsing it back out of the prompt would be guesswork
    because the text is neutralized, not delimited). It is subtracted from the
    request-header span, which is the block the user's text sits inside.

    ``user_offset`` is how many characters sit BETWEEN the request-header line
    and the user's text — the length of any context the caller prepended into
    the message region (a cancelled-turn preamble, subagent-failure notices,
    drained pending context). Those bytes are injected context, not user typing,
    so the user span starts ``user_offset`` chars in rather than flush against
    the header; the prepended context keeps its own attribution.

    Returns a label -> characters mapping. Zero-length blocks are omitted.
    Every character of ``prompt`` is accounted for exactly once, so the values
    sum to ``len(prompt)`` — a property the tests assert, and the reason
    ``unclassified`` exists rather than a silent drop.
    """
    if not prompt:
        return {}

    hits: list[tuple[int, str]] = []
    for label, pattern in _COMPILED:
        for match in pattern.finditer(prompt):
            hits.append((match.start(), label))

    # The user's own text is the one span of the prompt an attacker controls, so
    # its bounds must come from something they cannot influence. `user_chars` is
    # supplied by the caller, which knows the exact text it appended; the span
    # therefore runs from the end of the request-header LINE for that many
    # characters. Deriving the end from a marker search instead would be
    # forgeable in both directions: a message containing "\n\n(If " would move
    # the boundary into its own text, and one containing "[Memory ...]" would
    # book its bytes as memory while the header's bytes were credited to the
    # message.
    header_at = next(
        (
            m.start()
            for label, pat in _COMPILED
            if label == "request_header"
            for m in [pat.search(prompt)]
            if m
        ),
        None,
    )
    user_start = user_end = -1
    if user_span is not None:
        # Authoritative bounds from build_message — no reconstruction needed.
        user_start = max(0, min(user_span[0], len(prompt)))
        user_end = max(user_start, min(user_span[1], len(prompt)))
        user_chars = user_end - user_start
    elif header_at is not None and user_chars > 0:
        newline = prompt.find("\n", header_at)
        if newline != -1:
            # Skip past any context the caller prepended between the header and
            # the user's text (user_offset) before carving the user span.
            user_start = min(len(prompt), newline + 1 + max(0, user_offset))
            user_end = min(len(prompt), user_start + user_chars)

    # The reply-format contracts are appended AFTER the user's text, so search
    # for them past the span rather than from the start of the prompt.
    trailing = _TRAILING_CONTRACTS.search(prompt, max(user_end, 0))
    if trailing:
        hits.append((trailing.start(), REPLY_FORMAT_LABEL))
    hits.sort()

    if user_start >= 0:
        hits = [(pos, label) for pos, label in hits if not (user_start <= pos < user_end)]

    out: dict[str, int] = {}
    if not hits:
        # No markers at all: a bare prompt (minimal-context cron runs reach
        # here). Attribute what the caller told us and leave the rest visible.
        if user_chars:
            out[USER_LABEL] = min(user_chars, len(prompt))
        remainder = len(prompt) - out.get(USER_LABEL, 0)
        if remainder > 0:
            out[UNCLASSIFIED_LABEL] = remainder
        return out

    if hits[0][0] > 0:
        out[UNCLASSIFIED_LABEL] = hits[0][0]

    # Accumulate each block's span, carving out any overlap with the user span
    # so the user's bytes are credited to USER_LABEL EXACTLY where they sit.
    # No marker survives inside [user_start, user_end] (filtered above), so the
    # span lies within a single block — the one physically holding it, which is
    # NOT necessarily the request header: prepended marked context (e.g. a
    # drained "[Memory ...]") starts a block of its own, and the user's text
    # then sits inside THAT block. Subtracting user_chars from request_header by
    # count would leave the user's bytes mis-credited to memory and strip
    # unrelated header bytes instead.
    user_taken = 0
    for index, (start, label) in enumerate(hits):
        next_start = hits[index + 1][0] if index + 1 < len(hits) else len(prompt)
        # A block owns up to its own closer when it has one IN RANGE, otherwise up
        # to the next block. Bounding the search by ``next_start`` is what keeps a
        # wrapper (whose nested blocks open before it closes) behaving as before.
        end = next_start
        closer = _CLOSERS.get(label)
        if closer is not None:
            # The LAST match before the next opener, not the first. A block's
            # content can quote its own closer — a custom agent prompt that
            # documents the envelope it is injected into embeds
            # `[END AGENT SYSTEM PROMPT]` verbatim — and first-match would end the
            # block at that quotation, booking the rest of its real body as
            # `unclassified`. Last-match is right by construction: nothing of the
            # block follows its real closer, so any earlier occurrence in range is
            # content. Bounded by `next_start`, so this still cannot reach past the
            # next block.
            closed = None
            for match in closer.finditer(prompt, start, next_start):
                closed = match
            if closed is not None:
                end = closed.end()
                # The blank line a block emits right after its own closer is that
                # block's separator, not unattributed text. Keeping it here is what
                # makes the remainder a real gap instead of a two-character crumb
                # of `unclassified` after every single closed block.
                while end < next_start and prompt[end] in " \t\r\n":
                    end += 1
        seg = end - start
        if user_start >= 0:
            overlap = max(0, min(end, user_end) - max(start, user_start))
            seg -= overlap
            user_taken += overlap
        if seg > 0:
            out[label] = out.get(label, 0) + seg
        # The characters after this block's closer and before the next block
        # started. Naming them ``unclassified`` is the whole point: they used to be
        # billed to whichever block happened to precede them, which reads as a
        # confident measurement of something nobody measured.
        if end < next_start:
            gap = next_start - end
            if user_start >= 0:
                overlap = max(0, min(next_start, user_end) - max(end, user_start))
                gap -= overlap
                user_taken += overlap
            if gap > 0:
                out[UNCLASSIFIED_LABEL] = out.get(UNCLASSIFIED_LABEL, 0) + gap

    if user_taken > 0:
        out[USER_LABEL] = out.get(USER_LABEL, 0) + user_taken
    elif user_chars > 0:
        # No locatable span (a header-less prompt, so user_start < 0): fall back
        # to crediting the largest block by count, so the user's contribution is
        # never reported as zero when it isn't.
        host = max(out, key=lambda k: out[k]) if out else None
        if host is not None:
            taken = min(user_chars, out[host])
            out[host] -= taken
            out[USER_LABEL] = out.get(USER_LABEL, 0) + taken
            if out[host] <= 0:
                del out[host]

    return {label: size for label, size in out.items() if size > 0}

"""Marker and fence neutralization for untrusted prompt text.

Every untrusted payload the context builder places beside a trusted frame -- memory,
lessons, transcript rows, channel text, folder names, skill bodies, member briefings
-- is scrubbed here first, so text an agent or a third party wrote cannot close a
block early or forge one of the boundaries the model treats as authoritative. The
genuine frames are minted by the callers, after these scrubs run.

Two scrubs stay defined on :mod:`kiro_crew.context` because callers rebind them
there: ``_neutralize_structural_markers`` and ``_member_marker_spans``. Code in this
module reads both through the facade at call time. The ``[BOARD]`` tag screen
(``_board_safe_tag_name``) stays there too, beside the grant grammar it admits by.

New marker families, fences and normalizations belong here.
"""

from __future__ import annotations

import re
import unicodedata

# Delimiters that wrap untrusted Slack thread-parent text.
# The content is screened for injection and framed as UNTRUSTED DATA; these
# fence markers are also stripped from the content itself so a crafted parent
# message cannot forge the closing fence and "break out" of the block.
_THREAD_FENCE_OPEN = "<<<UNTRUSTED_THREAD_PARENT"
_THREAD_FENCE_CLOSE = ">>>END_UNTRUSTED_THREAD_PARENT"
_THREAD_FENCE_NEUTRALIZED = "[fence-marker-removed]"


def _fence_marker_regex(marker: str) -> re.Pattern[str]:
    """Compile a case-insensitive, separator-tolerant matcher for a fence marker.

    Each significant character of the marker may be separated by optional
    whitespace, and matching is case-insensitive. An underscore position is
    one separator run of any whitespace, underscore or hyphen, including an
    empty run: markers are matched on the normalized view, which removes
    default-ignorable characters and folds Unicode dashes to ``-``, so a
    separator may have been removed or folded before matching.

    The underscore run REPLACES the optional-whitespace joins on either side of
    it rather than sitting between them, so no two nullable classes are ever
    adjacent and matching stays linear in the input length.
    """
    separator = r"[\s_-]*"
    pieces: list[str] = []
    for ch in marker:
        if ch == "_":
            if pieces and pieces[-1] == r"\s*":
                pieces.pop()
            if not pieces or pieces[-1] != separator:
                pieces.append(separator)
            continue
        if pieces and pieces[-1] != separator:
            pieces.append(r"\s*")
        pieces.append(re.escape(ch))
    return re.compile("".join(pieces), re.IGNORECASE)


_THREAD_FENCE_OPEN_RE = _fence_marker_regex(_THREAD_FENCE_OPEN)
_THREAD_FENCE_CLOSE_RE = _fence_marker_regex(_THREAD_FENCE_CLOSE)

# Delimiters that wrap untrusted calendar/meeting metadata in a meetings agent's
# first message. Content inside the block is neutralized of every untrusted
# fence, so no fenced surface can close another surface's block either.
UNTRUSTED_CALENDAR_FENCE_OPEN = "<<<UNTRUSTED_CALENDAR_EVENT"
UNTRUSTED_CALENDAR_FENCE_CLOSE = ">>>END_UNTRUSTED_CALENDAR_EVENT"
_CALENDAR_FENCE_OPEN_RE = _fence_marker_regex(UNTRUSTED_CALENDAR_FENCE_OPEN)
_CALENDAR_FENCE_CLOSE_RE = _fence_marker_regex(UNTRUSTED_CALENDAR_FENCE_CLOSE)

# Delimiters that wrap the agent's own checklist task texts when the dashboard
# re-states them to a fresh or live session (``_ChatSlot.todo_recovery_prompt``
# / ``todo_sync_prompt``). A task text is whatever the agent typed into its
# todo_list tool, which can have been copied from a file or a page, so it is
# re-sent as data inside this fence, never as an instruction.
UNTRUSTED_TODO_FENCE_OPEN = "<<<UNTRUSTED_TODO_TEXT"
UNTRUSTED_TODO_FENCE_CLOSE = ">>>END_UNTRUSTED_TODO_TEXT"
_TODO_FENCE_OPEN_RE = _fence_marker_regex(UNTRUSTED_TODO_FENCE_OPEN)
_TODO_FENCE_CLOSE_RE = _fence_marker_regex(UNTRUSTED_TODO_FENCE_CLOSE)

_UNTRUSTED_FENCE_RES: tuple[re.Pattern[str], ...] = (
    _THREAD_FENCE_CLOSE_RE,
    _THREAD_FENCE_OPEN_RE,
    _CALENDAR_FENCE_CLOSE_RE,
    _CALENDAR_FENCE_OPEN_RE,
    _TODO_FENCE_CLOSE_RE,
    _TODO_FENCE_OPEN_RE,
)


def _neutralize_fence_markers(text: str) -> str:
    """Replace Unicode-normalized variants of every untrusted fence in *text*.

    Covers the thread-parent, calendar-event and todo-text fences, open and close. The
    shared marker matcher supplies NFKC, default-ignorable removal, and
    original-coordinate spans; the replacement remains fence-specific.
    """
    spans = _marker_spans(text, _UNTRUSTED_FENCE_RES)
    return _apply_marker_spans(text, spans, _THREAD_FENCE_NEUTRALIZED)


# Primary structural boundary markers that ``build_message`` uses to separate
# TRUSTED framing (the agent system prompt, the critical-rules block, the
# session-context wrapper, the current-user-request header) from the UNTRUSTED
# content concatenated into the SAME single-turn prompt string. Because the
# prompt is delivered as one turn (no first-class role=system/role=user
# channel), these static, public markers are the ONLY boundary the model has.
# Untrusted text mixed into the prompt — memory / lessons / history / episodic /
# channel context / the user's own turn — is scrubbed of these markers before
# concatenation (the same intent as the thread fence above), so a crafted
# closing marker such as ``[END OF SESSION CONTEXT]`` followed by a forged
# ``[CURRENT USER REQUEST ...]`` cannot "break out" of its block and inject
# instructions the model treats as authoritative (CWE-94 / CWE-116).
#
# Each matcher is BRACKET-ANCHORED and WORD-level: whitespace is tolerated
# between the fixed words (``\s*``, which also spans newlines and — since
# ``_neutralize_structural_markers`` first drops zero-width chars — a merged
# ``ENDOF``) and at the bracket edges. The two variable-tail markers match only
# the distinctive HEAD — ``[`` + phrase + a required hyphen separator (the
# neutralizer normalizes multibyte punctuation and folds every Unicode dash to
# an ASCII hyphen first) — and deliberately do NOT try to match the variable
# tail up to the closing ``]``. This (1) catches real / spaced / mixed-case /
# unicode-separator / zero-width / multi-line forgeries, (2) leaves ordinary
# bracketed prose such as ``[Session Context](url)`` or ``[Critical Rules]``
# untouched (no hyphen separator ⇒ no match), and (3) stays linear with no tail
# to exploit (a bounded tail invited newline/length bypasses; CWE-1333
# backtracking is avoided — no adjacent variable-width quantifiers). The
# ``[SESSION CONTEXT …]`` OPEN marker is intentionally omitted: forging it only
# opens a "background, do not act on this" block (a de-escalation), so it is not
# a breakout vector. ``_fence_marker_regex`` is left untouched for the
# underscore-only thread fence.
_REPLY_FORMAT_RULES_MARKER = "[REPLY FORMAT RULES]"
_REPLY_FORMAT_RULES_RE = re.compile(
    r"\[\s*REPLY\s*FORMAT\s*RULES\s*\]",
    re.IGNORECASE,
)

_STRUCTURAL_MARKER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\s*AGENT\s*SYSTEM\s*PROMPT\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*END\s*AGENT\s*SYSTEM\s*PROMPT\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*END\s*CRITICAL\s*RULES\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*END\s*OF\s*SESSION\s*CONTEXT\s*\]", re.IGNORECASE),
    _REPLY_FORMAT_RULES_RE,
    re.compile(r"\[\s*CRITICAL\s*RULES\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*CURRENT\s*USER\s*REQUEST\s*[-]{1,2}", re.IGNORECASE),
    # Forging this opener escalates attacker text above agent-prompt style rules,
    # so the genuine frame is minted only after the untrusted-context scrub.
    re.compile(r"\[\s*RESPONSE\s*PREFERENCES\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*END\s*RESPONSE\s*PREFERENCES\s*\]", re.IGNORECASE),
    # Post-compaction skills re-injection boundary. Unlike the ``[SESSION
    # CONTEXT …]`` OPEN marker (omitted above because forging it only opens a
    # "background, do not act on this" block), forging THIS open marker is an
    # escalation: it presents attacker-chosen text as the platform-supplied
    # skills index — a catalog of capability names and on-disk paths the model
    # is told to read. Head-anchored with the required hyphen separator, per
    # the variable-tail convention above.
    re.compile(r"\[\s*REINJECTED\s*AFTER\s*COMPACTION\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*END\s*REINJECTED\s*\]", re.IGNORECASE),
    # Folder steering's own frame. Forging the opener presents attacker text
    # (a channel message, a memory line, a steering BODY) as operator-selected
    # folder rules "to follow as you would project steering" -- an escalation.
    # The genuine section is therefore minted AFTER this scrub, by
    # ``build_message`` (fresh session) and the compaction-reinjection leg,
    # never inside the scrubbed session-context tail. Head-anchored with the
    # required hyphen separator, per the variable-tail convention above.
    re.compile(r"\[\s*FOLDER\s*STEERING\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*END\s*FOLDER\s*STEERING\s*\]", re.IGNORECASE),
    # The checklist blocks the runner prepends for a fresh or live session
    # (``_ChatSlot.todo_recovery_prompt`` / ``todo_sync_prompt``). Minted AFTER
    # the egress scrub, like folder steering, so a copy in a fetched page or a
    # tool result is neutralized and only the gateway's own block carries the
    # frame. The em dash the block uses folds to ``-`` before matching.
    re.compile(r"\[\s*TASK\s*CHECKLIST\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*END\s*TASK\s*CHECKLIST\s*\]", re.IGNORECASE),
    # The interrupted-turn restore (``build_interrupted_turn_preamble``). Its frame
    # names the request inside it as the one the model is to carry on with, so a
    # copy planted in a fetched page or a channel message would hand attacker
    # text that authority. Minted AFTER the egress scrub by the runner, like the
    # checklist, with its own payload scrubbed first.
    re.compile(r"\[\s*INTERRUPTED\s*TURN\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*END\s*INTERRUPTED\s*TURN\s*\]", re.IGNORECASE),
)
_STRUCTURAL_MARKER_NEUTRALIZED = "[marker-removed]"

# Unicode Default_Ignorable_Code_Point includes more than category Cf. Marker
# matching removes these code points from its VIEW only (the original text is
# unchanged unless the surrounding marker matches), closing invisible-split
# variants such as U+034F and variation selectors without mutating prose.
_MARKER_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2065, 0x2065),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _is_marker_ignorable(ch: str) -> bool:
    if unicodedata.category(ch) == "Cf":
        return True
    codepoint = ord(ch)
    return any(start <= codepoint <= end for start, end in _MARKER_IGNORABLE_RANGES)


# Forgeable member-authority markers, scrubbed from every VARIABLE payload the
# member section frames (description, triggers, rules, briefing). The genuine
# headers are minted by ``_build_member_section``'s own f-strings AFTER this
# scrub, so a forged ``[PERMANENT RULES — …]`` planted in the agent-writable
# briefing (steered external content) cannot render as the user-owned layer.
# Deliberately NOT added to ``_STRUCTURAL_MARKER_RES``: that scan runs over the
# whole session-context tail, which CONTAINS the genuine member section, so a
# global pattern would neutralize the real header along with the forgery —
# content-time scrubbing is the only placement that distinguishes them.
# Head-anchored with the required separator, per the variable-tail convention
# on ``_STRUCTURAL_MARKER_RES``.
_MEMBER_MARKER_RES: tuple[re.Pattern[str], ...] = (
    # Every minted header in BOTH shapes: the exact closing-bracket form and
    # the hyphen-tail form (`[HOW YOU WORK — override]`), since a forged
    # variant of either still reads authoritative to the model. The scrub runs
    # on a normalized view (dashes folded to '-'), so one hyphen class covers
    # every Unicode dash.
    re.compile(r"\[\s*MEMBER\s*IDENTITY\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*MEMBER\s*IDENTITY\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*END\s*MEMBER\s*IDENTITY\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*END\s*MEMBER\s*IDENTITY\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*HOW\s*YOU\s*WORK\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*HOW\s*YOU\s*WORK\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*PERMANENT\s*RULES\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*PERMANENT\s*RULES\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*CURRENT\s*ASSIGNMENT\s*[-]{1,2}", re.IGNORECASE),
    re.compile(r"\[\s*CURRENT\s*ASSIGNMENT\s*\]", re.IGNORECASE),
)


def _member_normalized_view(text: str) -> str:
    """The member scrub's historical whole-string normalization.

    NFKC-fold (fullwidth/compatibility confusables like
    ``［ＰＥＲＭＡＮＥＮＴ ＲＵＬＥＳ］`` collapse to their ASCII forms), then
    drop Unicode default-ignorables, fold ``_MULTIBYTE_TABLE`` punctuation,
    and map every Unicode dash (``Pd``) to ASCII ``-``. NFKC runs first
    because it maps compatibility glyphs the category filters never touch;
    the ignorable/dash passes stay because NFKC preserves grapheme joiners,
    variation selectors, and most dashes. Kept as the fail-closed floor for
    :func:`_scrub_member_payload`.
    """
    return "".join(
        "-" if unicodedata.category(folded) == "Pd" else folded
        for ch in unicodedata.normalize("NFKC", text)
        if not _is_marker_ignorable(ch)
        for folded in ch.translate(_MULTIBYTE_TABLE)
    )


def _scrub_member_payload(text: str) -> str:
    """Neutralize member-authority markers in an untrusted payload.

    Detection runs on a normalized view (NFKC + ignorable drop + ``Pd`` fold, see
    :func:`_member_marker_spans`) so a confusable forgery
    (``[PERM<zwsp>ANENT RULES‐``) cannot slip past the ASCII patterns — but
    the rewrite is SPAN-LOCAL in the ORIGINAL text: only matched marker spans
    are replaced, so legitimate fullwidth/compatibility characters, zero-width
    joiners and Unicode dashes outside a forgery survive byte-exact. A
    permanent rule protecting ``Ａ.txt`` reaches the member naming ``Ａ.txt``,
    not its NFKC fold (the whole-payload normalized injection this replaces
    handed the member a subtly different safety boundary than the user wrote).

    FAIL-CLOSED FLOOR: the span-scrubbed result is re-checked against the
    historical whole-string normalized view; if any marker pattern still
    matches there, the scrub degrades to exactly that historical behavior —
    normalize the whole payload and substitute every match. A mapping defect
    can therefore cost fidelity, never admit a forgery: every return value
    either passes the whole-string detector clean or IS its output.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    scrubbed = _apply_marker_spans(text, ctx._member_marker_spans(text))
    residue = _member_normalized_view(scrubbed)
    if any(pattern.search(residue) for pattern in _MEMBER_MARKER_RES):
        for pattern in _MEMBER_MARKER_RES:
            residue = pattern.sub(_STRUCTURAL_MARKER_NEUTRALIZED, residue)
        return residue
    return scrubbed


def _merge_overlapping_spans(raw: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and merge overlapping/adjacent match spans (shared by both views)."""
    if not raw:
        return []
    raw.sort()
    merged: list[tuple[int, int]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _marker_spans(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> list[tuple[int, int]]:
    """Merged *patterns* matches in *text*, in original coordinates.

    Matching runs against a normalized view (fold ``_MULTIBYTE_TABLE``
    punctuation, drop format/zero-width chars, and map Unicode dashes to
    ASCII) with an index map back to the original offsets. Callers can enforce
    one boundary class without rewriting unrelated trusted markers.
    """
    if text.isascii():  # pure ASCII cannot contain confusables — match directly
        raw = [m.span() for pattern in patterns for m in pattern.finditer(text)]
    else:
        # Compatibility-normalized matching view + origin map (normalized char
        # i came from original index ``origin[i]``). NFKC folds fullwidth and
        # other compatibility glyphs; default-ignorables are removed from the
        # view only, so original prose stays byte-identical unless a marker
        # actually matches.
        norm: list[str] = []
        origin: list[int] = []
        cursor = 0
        # ASCII is unchanged by every normalization below. Copy entire runs in
        # C instead of paying the Unicode pipeline per character whenever one
        # non-ASCII character appears anywhere in a large prompt.
        for match in re.finditer(r"[^\x00-\x7f]", text):
            idx = match.start()
            norm.append(text[cursor:idx])
            origin.extend(range(cursor, idx))
            cursor = idx + 1
            for compatible in unicodedata.normalize("NFKC", match.group()):
                if _is_marker_ignorable(compatible):
                    continue
                folded = compatible.translate(_MULTIBYTE_TABLE)
                for candidate in folded:
                    norm.append("-" if unicodedata.category(candidate) == "Pd" else candidate)
                    origin.append(idx)
        norm.append(text[cursor:])
        origin.extend(range(cursor, len(text)))

        norm_str = "".join(norm)
        raw = []
        for pattern in patterns:
            for match in pattern.finditer(norm_str):
                start, end = match.span()
                # Through the last matched char, in original coordinates.
                raw.append((origin[start], origin[end - 1] + 1))

    return _merge_overlapping_spans(raw)


def _structural_marker_spans(text: str) -> list[tuple[int, int]]:
    """Merged spans of all forgeable primary boundaries in original coords.

    Split out from :func:`_neutralize_structural_markers` so the same match set
    drives both rewriting and user-offset mapping.
    """
    return _marker_spans(text, _STRUCTURAL_MARKER_RES)


def _apply_marker_spans(
    text: str,
    spans: list[tuple[int, int]],
    replacement: str = _STRUCTURAL_MARKER_NEUTRALIZED,
) -> str:
    """Rewrite each span of *text* with *replacement*."""
    if not spans:
        return text
    out: list[str] = []
    cursor = 0
    for s, e in spans:
        if s < cursor:
            continue
        out.append(text[cursor:s])
        out.append(replacement)
        cursor = e
    out.append(text[cursor:])
    return "".join(out)


def _map_offset_through_spans(off: int, spans: list[tuple[int, int]]) -> int:
    """Map an offset in the ORIGINAL text to its offset after neutralization.

    Each rewritten span changes the length by ``len(placeholder) - (end-start)``,
    so an offset shifts by the sum of the deltas of every span that ends before
    it. An offset that falls INSIDE a rewritten span (the user's text opening
    with a forged marker, say) clamps to that span's start in output coords —
    the original bytes there are gone.
    """
    delta = 0
    placeholder = len(_STRUCTURAL_MARKER_NEUTRALIZED)
    for s, e in spans:
        if e <= off:
            delta += placeholder - (e - s)
        elif s < off:
            return s + delta  # inside the span: clamp to where it now starts
        else:
            break
    return off + delta


def neutralize_untrusted_text(text: str) -> str:
    """Neutralize untrusted fence markers and primary boundary markers in *text*.

    Public entry point for surfaces outside this module that frame untrusted
    content inside an untrusted-data fence: the result carries no fence marker
    (thread-parent or calendar-event) and no forgeable prompt boundary marker.
    Span-local, like both underlying scrubs.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    return ctx._neutralize_structural_markers(_neutralize_fence_markers(text))


# Frames a stored lesson must not carry into the per-message lessons block: the
# lesson frames, which a lesson could close early and then speak outside, and the
# skill frame the block follows.
_TURN_LESSON_FRAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\s*LEARNED\s*(?:CORRECTIONS|EXPERIENCE)\b", re.IGNORECASE),
    re.compile(r"\[\s*END\s*OF\s*LEARNED\s*(?:CORRECTIONS|EXPERIENCE)\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*SKILL\s*:", re.IGNORECASE),
    re.compile(r"\[\s*END\s*OF\s*SKILL\s*\]", re.IGNORECASE),
)


def _scrub_turn_lesson(text: str) -> str:
    """One stored lesson, made safe to place in the per-message lessons block.

    A lesson is untrusted: a rule can be inferred from a conversation or imported.
    Besides the primary boundary markers it loses the member-authority markers a
    private member runs under and the lesson and skill frame markers, so it can
    neither close its own frame nor open one that reads as a member rule or a
    skill. Span-local, like the scrubs it composes.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    framed = _apply_marker_spans(text, _marker_spans(text, _TURN_LESSON_FRAME_RES))
    scrubbed = ctx._neutralize_structural_markers(_scrub_member_payload(framed))
    return scrubbed.translate(_MULTIBYTE_TABLE)


def _neutralize_reply_format_markers(text: str) -> str:
    """Neutralize only reply-format headers in an assembled prompt segment.

    This is the centralized minting guard: all content already assembled before
    the trusted reply-format block passes through it, regardless of which
    current or future source produced that content. Other trusted structural
    markers remain untouched.
    """
    spans = _marker_spans(text, (_REPLY_FORMAT_RULES_RE,))
    return _apply_marker_spans(text, spans)


# kiro-cli task_executor slices strings at fixed byte offsets (e.g. 4096).
# Multi-byte UTF-8 chars straddling the boundary cause a Rust panic:
#   "byte index 4096 is not a char boundary; it is inside '—'"
# Workaround: replace common multi-byte punctuation with ASCII equivalents.
# TODO: revert once kiro-cli ships its truncate_safe fix.
_MULTIBYTE_TABLE = str.maketrans(
    {
        "\u2014": "--",  # em dash
        "\u2013": "-",  # en dash
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u2026": "...",  # ellipsis
        "\u00a0": " ",  # non-breaking space
        "\u2022": "-",  # bullet
        "\u2192": "->",  # rightwards arrow (→) — caused 5 kiro-cli panics
        "\u2190": "<-",  # leftwards arrow (←)
        "\u2194": "<->",  # left right arrow (↔)
        "\u21d2": "=>",  # rightwards double arrow (⇒)
        "\u2713": "[x]",  # check mark (✓)
        "\u2717": "[ ]",  # ballot x (✗)
        "\u00d7": "x",  # multiplication sign (×)
        # Known gap: accented chars (e.g. \u00e9) and emoji are not replaced here.
        # They are legitimate content; stripping them would be lossy. The real fix
        # is kiro-cli's truncate_safe.
    }
)

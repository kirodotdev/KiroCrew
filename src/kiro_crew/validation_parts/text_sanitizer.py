"""Hidden-character stripping and the sanitizers every string and tool response passes.

:func:`sanitize_string` is the pipeline every validated string field runs through;
:func:`sanitize_json_values` walks decoded JSON the same way, and
:func:`sanitize_response` sanitizes and truncates a tool response.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from kiro_crew.validation_parts.input_bounds import MAX_RESPONSE_LEN

# Unicode categories :func:`strip_hidden_unicode` strips, minus the
# ``_ALLOWED_CONTROL`` / ``_ALLOWED_FORMAT`` carve-outs below. ``Cf`` (format)
# IS included — it is stripped by default and only the shaping characters named
# in ``_ALLOWED_FORMAT`` get through, and only next to non-ASCII text.
# Fail-closed is required here rather than aesthetic: this sanitizer runs BEFORE
# credential redaction, so any invisible character it preserves can be inserted into a
# credential to defeat ``redact_credentials``' patterns and carry a recoverable
# secret into the dashboard and the notification JSONL. An allowlist means a
# newly-assigned or simply un-enumerated ``Cf`` codepoint is blocked instead of
# silently becoming an evasion vector.
#
# ``Co`` (private use) is excluded: Nerd Fonts and terminal themes carry real
# icon glyphs there, and unlike ``Cf`` those are visible, so they cannot hide a
# credential from a human reader.
_HIDDEN_CATEGORIES = frozenset(
    {
        "Cc",  # control (except \n \r \t)
        "Cf",  # format — see _ALLOWED_FORMAT for the narrow exceptions
        "Cs",  # surrogate — never valid in well-formed text
    }
)

# The ONLY format characters allowed through. Each has a real text-shaping job
# that scripts and emoji sequences cannot express without it, so removing them
# corrupts user content (``dashboard/chat_folders_icons.py`` treats U+200D as a
# meaningful emoji modifier, and ``test_validation.TestStripHiddenUnicode`` pins
# ZWNJ/ZWJ survival here).
#
# Everything else in ``Cf`` stays denied, including ZWSP U+200B, the word joiner
# and invisible operators U+2060-2064, BOM U+FEFF, the bidi embedding/override/
# isolate controls (Trojan Source, CVE-2021-42574), interlinear annotation
# controls, and SOFT HYPHEN U+00AD.
#
# NOTE: the variation selectors (U+FE00-FE0F) are category ``Mn``, not ``Cf``,
# so they were never subject to this rule and need no entry here.
_ALLOWED_FORMAT = frozenset(
    {
        "\u200c",  # ZWNJ — required by Persian / Arabic / Indic orthography
        "\u200d",  # ZWJ — welds emoji sequences; required by Indic scripts
        "\u200e",  # LRM — ordinary mark in mixed-direction text
        "\u200f",  # RLM — ordinary mark in mixed-direction text
    }
)

# Specific chars to always allow even if in a hidden category
_ALLOWED_CONTROL = frozenset({"\n", "\r", "\t"})


# ── String Sanitization ──


def strip_hidden_unicode(text: str) -> str:
    """Remove hidden Unicode, preserving only script-essential shaping marks.

    Removes control characters (except \\n, \\r, \\t), surrogates, and category
    ``Cf`` — which covers ZWSP, BOM, the word joiner and invisible operators,
    and the bidi override/isolate controls behind Trojan Source
    (CVE-2021-42574).

    The four marks in :data:`_ALLOWED_FORMAT` are kept ONLY when at least one
    immediate neighbour is non-ASCII. They exist to shape non-ASCII text —
    emoji sequences, Arabic / Persian / Indic orthography — so between two
    ASCII characters they have no rendering effect and their only practical use
    is hiding a credential from redaction: this function runs BEFORE
    ``redact_credentials``, so a surviving invisible inside
    ``AKIA<ZWJ>IOSFODNN7EXAMPLE`` would defeat its patterns and carry a
    recoverable secret to the dashboard and the notification JSONL.

    Tradeoff, accepted deliberately: an LRM/RLM placed between two ASCII
    characters inside otherwise-bidi text is also dropped. That usage is rare
    in tool output, and a per-string "contains any RTL" test would let an
    attacker re-enable the mark by planting one non-ASCII character elsewhere
    in the same response — so the neighbour test is the one that actually
    closes the bypass.
    """
    # Pass 1: drop every hidden character EXCEPT the shaping marks, so the
    # neighbour test below only ever sees characters that actually SURVIVE.
    # Testing against the raw input let a stripped character vouch for a mark
    # (``AKIA<ZWSP><ZWJ>IOSF…``: the ZWSP is removed, but the ZWJ saw it as a
    # non-ASCII neighbour and stayed, leaving the credential unredactable).
    kept = _drop_hidden_except_marks(text)
    # Pass 2: a shaping mark survives only if the nearest surviving
    # NON-MARK character on one side is non-ASCII. Skipping over adjacent
    # marks is what stops a run of them from vouching for each other —
    # ``"\u200d".isascii()`` is False, so ``AKIA<ZWJ><ZWJ>IOSF…`` would
    # otherwise keep both and stay unredactable.
    total = len(kept)
    out: list[str] = []
    for i, ch in enumerate(kept):
        if ch not in _ALLOWED_FORMAT:
            out.append(ch)
            continue
        left = i - 1
        while left >= 0 and kept[left] in _ALLOWED_FORMAT:
            left -= 1
        right = i + 1
        while right < total and kept[right] in _ALLOWED_FORMAT:
            right += 1
        prev_ch = kept[left] if left >= 0 else ""
        next_ch = kept[right] if right < total else ""
        if (prev_ch and not prev_ch.isascii()) or (next_ch and not next_ch.isascii()):
            out.append(ch)
    return "".join(out)


def _drop_hidden_except_marks(text: str) -> list[str]:
    """Drop every hidden character except the shaping marks in ``_ALLOWED_FORMAT``."""
    return [
        ch
        for ch in text
        if ch in _ALLOWED_CONTROL
        or ch in _ALLOWED_FORMAT
        or unicodedata.category(ch) not in _HIDDEN_CATEGORIES
    ]


def normalize_unicode(text: str) -> str:
    """NFC-normalize Unicode text to canonical form."""
    return unicodedata.normalize("NFC", text)


def sanitize_string(text: str) -> str:
    """Full sanitization pipeline: drop hidden → normalize → strip hidden → trim edges.

    Dropping hidden characters first lets combining marks compose across a
    removed character. The shaping marks are judged only after NFC, because
    NFC changes their neighbours (``e`` + U+0301 becomes non-ASCII ``é``, and
    U+212A KELVIN SIGN becomes ASCII ``K``).
    """
    text = normalize_unicode("".join(_drop_hidden_except_marks(text)))
    return _trim_edges(strip_hidden_unicode(text))


def _trim_edges(text: str) -> str:
    """Trim edge whitespace and the shaping marks it would strand, in one pass.

    A shaping mark in an edge run survives only when it touches the kept text
    and that text's edge character is non-ASCII. Trimming whitespace alone can
    leave a mark beside ASCII only, which the next ``sanitize_string`` call
    would then remove.
    """

    def trimmable(ch: str) -> bool:
        return ch.isspace() or ch in _ALLOWED_FORMAT

    start, end = 0, len(text)
    while start < end and trimmable(text[start]):
        start += 1
    while end > start and trimmable(text[end - 1]):
        end -= 1
    if start == end:
        return ""
    if not text[start].isascii():
        while start > 0 and text[start - 1] in _ALLOWED_FORMAT:
            start -= 1
    if not text[end - 1].isascii():
        while end < len(text) and text[end] in _ALLOWED_FORMAT:
            end += 1
    return text[start:end]


def sanitize_json_values(value: Any) -> Any:
    """Recursively sanitize every string (keys included) in decoded JSON.

    Schema-level sanitization sees a JSON document only as one opaque string,
    where an escape like ``\\u200b`` is plain ASCII and passes untouched — the
    hidden character only materializes when ``json.loads`` decodes it, AFTER
    the sanitizer ran. Any handler that decodes caller-supplied JSON must walk
    the decoded structure through this before acting on it, or an invisible
    character smuggled inside a credential defeats downstream redaction.
    """
    if isinstance(value, str):
        return sanitize_string(value)
    if isinstance(value, dict):
        return {sanitize_json_values(k): sanitize_json_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_json_values(item) for item in value]
    return value


# ── Response Sanitization ──


def sanitize_response(text: str, max_len: int = MAX_RESPONSE_LEN) -> str:
    """Sanitize and truncate a tool response before returning to caller."""
    text = sanitize_string(text)
    if len(text) > max_len:
        # Truncation drops the TAIL, and tail-anchored payloads live there —
        # a session directive's marker is the last line, so a response over the
        # cap loses its effect entirely. Never silent.
        logging.getLogger("kiro_crew.validation").warning(
            "tool response truncated: %d chars over the %d cap — a "
            "tail-anchored marker (e.g. a session directive) would be lost",
            len(text) - max_len,
            max_len,
        )
        text = text[:max_len] + "\n…[response truncated]"
    return text

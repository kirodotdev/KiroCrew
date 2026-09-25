"""Credential redaction on every output path.

This is the OUTPUT side of the module, and the widest external surface in it:
the batch redactors run on every path that persists, displays or forwards text,
so a name here is called from most of the codebase rather than from one caller.

The alternation and its pre-filter are ONE unit. The pre-filter is a documented
strict superset of the alternation, and the batch redactor SKIPS the scan
entirely when the pre-filter returns False, so an input the alternation would
have matched but the pre-filter rejects is a silent leak rather than a missed
optimisation. They are declared adjacent, with the comment that records the
relation, and the superset property is asserted by test.

The entropy machinery behind them answers a different question from the
alternation: a bare high-entropy run carries no marker to anchor on, so it is
judged by shape -- length, character classes, entropy, decodability -- and every
gate is a separate predicate so a refusal can name which one fired.
"""

from __future__ import annotations

import base64
import bisect
import contextvars
import functools
import hashlib
import hmac
import json
import math
import os
import posixpath
import re
import secrets
import zlib
from collections import Counter
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import NamedTuple

from kiro_crew.credential_patterns import AWS_KEY_ID, JWT_MULTI_SEGMENT
from kiro_crew.security.redaction_switch import credential_pass_bypassed

# Standard replacement tag for a redacted credential. Shared between the batch
# redactor (`redact_credentials`) and the streaming fail-closed path
# (`StreamRedactor.feed`) so the on-the-wire marker is identical everywhere.
# Defined ABOVE `_CREDENTIAL_PATTERNS` because the key-anchored branches embed
# the registered tags as an atom of their value group (`_CREDENTIAL_TAG_ATOM`).
_REDACTED_CREDENTIAL_TAG = "[REDACTED: credential]"

# Public alias for modules that must emit the SAME tag rather than duplicate the
# literal — e.g. the pptx-maker preview, which excises a credential-bearing bitmap
# itself because this module's redactor recognises a narrower token set than that
# scan matches.
REDACTED_CREDENTIAL_TAG = _REDACTED_CREDENTIAL_TAG

#: Replacement tag for pass 2 (a base64-encoded credential). DISTINCT from
#: ``_REDACTED_CREDENTIAL_TAG`` and deliberately not a superstring of it, so a
#: consumer counting one tag does not accidentally match the other. Kept PRIVATE:
#: consumers should ask ``CREDENTIAL_REDACTION_TAGS`` below rather than name
#: individual tags, which is the whole point of that registry.
_REDACTED_ENCODED_CREDENTIAL_TAG = "[REDACTED: encoded credential]"

#: EVERY tag :func:`redact_credentials` can substitute for a credential, owned
#: HERE beside the passes that emit them rather than enumerated by each caller.
#: A consumer that needs to answer "did the CREDENTIAL redactor replace something
#: in this text" must check all of them: pass 1 (plaintext patterns) and pass 3
#: (bare secret runs) write ``_REDACTED_CREDENTIAL_TAG``, pass 2 (base64-encoded)
#: writes ``_REDACTED_ENCODED_CREDENTIAL_TAG``.
#:
#: Scope is deliberately CREDENTIALS ONLY, and a consumer must not read it as "was
#: this text rewritten at all". :func:`redact_exfiltration_urls` is a separate
#: rewriter that substitutes ``[REDACTED: suspicious URL to <domain>]`` -- a
#: variable string, so it is prefix-matched rather than compared, which is why it
#: is not a member here. Its stable prefix is exported as
#: :data:`kiro_crew.security.exfil.EXFILTRATION_REDACTION_TAG_PREFIX` (beside
#: the rewriter itself), and a consumer that needs the full "was this text
#: rewritten" answer must check that constant by prefix ALONGSIDE this tuple --
#: the dashboard chat notice does exactly that.
#:
#: This tuple exists so the enumeration lives beside the tags instead of at the
#: call site, where it silently misses a tag and under-reports redactions on the
#: dashboard chat notice. Co-locating it means a NEW tag is added next to the list
#: that must name it; ``test_every_redaction_tag_constant_is_registered`` fails if
#: one is added without registering it, so the drift cannot happen silently.
#:
#: Invariant relied on by callers that SUM per-tag counts: no tag is a substring
#: of another, so one substitution cannot be counted twice.
CREDENTIAL_REDACTION_TAGS = (_REDACTED_CREDENTIAL_TAG, _REDACTED_ENCODED_CREDENTIAL_TAG)

#: The registered tags as one regex atom, for the value group of every pattern
#: that redacts a VALUE and keeps the key that names it (the key-anchored
#: branches of `_CREDENTIAL_PATTERNS`, the `token=` parameter of pass 4). Those
#: value classes all stop at whitespace, and every tag carries an interior space,
#: so without this atom a re-run over the redactor's own output would match the
#: tag's `[REDACTED:` head as a new value. With it, the value is EITHER a whole
#: tag plus whatever is glued to it (`TAG<class>*`) OR an ordinary run
#: (`<class>+`), so `_value_is_credential_tag` can decide by byte identity of the
#: ENTIRE value: a bare tag is left alone, a tag with bytes glued to its `]` is
#: redacted whole (those bytes were never certified by anything), and a tag
#: followed by the class's own boundary -- a space, a quote, `,`, `}` -- is a
#: bare tag with an ordinary tail. Presence-only consumers
#: (`_contains_fixed_credential`) are unchanged: wherever the atom matches, the
#: plain class matched already; only the extent of the match differs. The atom
#: is a RUN of one or more whole tags, so two adjacent tags -- two credentials
#: that stood side by side inside one value -- are one value the predicate reads
#: whole, never a tag plus a head cut at the second tag's interior space.
_CREDENTIAL_TAG_ATOM = "(?:" + "|".join(re.escape(tag) for tag in CREDENTIAL_REDACTION_TAGS) + ")+"

#: Every STRICT prefix of a registered tag (one byte short of the literal and
#: shorter), longest first, for the streaming grammar only. A chunk boundary can
#: fall anywhere inside a tag standing as a redacted value, and past the tag's
#: interior space neither the tag atom (which needs the whole literal) nor the
#: value class (which stops at the space) recognises the tail as the in-progress
#: value it is. Generated from the registry, so a tag added there is held
#: without a second edit; a prefix shared by two tags appears once.
_CREDENTIAL_TAG_PREFIX_ATOM = (
    "(?:"
    + "|".join(
        re.escape(prefix)
        for prefix in sorted(
            {tag[:k] for tag in CREDENTIAL_REDACTION_TAGS for k in range(1, len(tag))},
            key=lambda p: (-len(p), p),
        )
    )
    + ")"
)

#: The optional quote of a key-anchored LABEL -- the one closing a JSON key and
#: the one opening the value -- bare or ESCAPED (``\"``): the two spellings a
#: quoted pair has, standing in its own document and embedded in an enclosing
#: string literal. One atom for the four key-anchored branches and
#: ``_AWS_LABEL_RULES``, so the label rule is one rule. The value scanner
#: (:func:`scan_keyed_value`) reads the opener's spelling itself and finds the
#: close in the same one.
_LABEL_QUOTE = r"(?:\\?[\"'])?"

#: The key spellings of the three AWS key-anchored branches (the Bearer branch
#: has its own hold anchor in the stream, `_BEARER_ANCHOR_PARTIAL_RE`).
_KEY_ANCHORED_KEYS = (
    "SecretAccessKey",
    "aws_secret_access_key",
    "SessionToken",
    "aws_session_token",
    "AccessKeyId",
    "aws_access_key_id",
)

#: A key-anchored LABEL cut short at the text's end BEFORE its separator has
#: arrived -- after the key, after the quote closing a JSON key, after the
#: whitespace before the separator, or at the lone backslash of an escaped
#: closing quote still to come. From the separator on, the anchor branch
#: matches and :func:`scan_keyed_value` says whether the value is still coming
#: (``KeyedValue.pending``), so the stream holds from the key either way
#: (`_key_anchored_hold_start`). The Bearer header's key is here too, folded as
#: its branch is: the stream's own Bearer anchor is STRONG and so requires the
#: separator, and the tails before it are this WEAK hold's.
_LABEL_TAIL = r"(?:\\|" + _LABEL_QUOTE + r"\s*)"
_KEY_ANCHORED_LABEL_TAIL_RE = re.compile(
    "(?:" + "|".join(_KEY_ANCHORED_KEYS) + "|(?i:Authorization))" + _LABEL_TAIL + r"\Z"
)


class KeyedValue(NamedTuple):
    """The extent of a key-anchored VALUE, as :func:`scan_keyed_value` reads it.

    ``start``/``end`` bound the claim: from the first byte after the opener
    (leading escaped whitespace included) to the closing quote's index, the
    line's end, or the text's end. ``closes`` says a closing quote stands at
    ``end`` (always True for an unquoted value: there is no close to write).
    ``opener`` is the opening quote AS WRITTEN -- ``"``, ``'``, ``\\"`` or
    ``\\'``, or ``""`` for an unquoted value -- which is what a claim running to
    an unterminated line's end writes as its close. ``pending`` says the scan
    ran out of text with the value possibly continuing (a quote still open, an
    unquoted run with no terminator yet, an opener or a lone backslash with
    nothing after it): the stream holds the pair from its key while it is set.
    """

    start: int
    end: int
    closes: bool
    opener: str
    pending: bool


#: Escape letters a serializer writes for whitespace. An escape pair carrying one
#: INSIDE a value is the inner line's end (the rule for an embedded value); a
#: run of them at the value's HEAD is leading whitespace in the value's
#: encoding, consumed with the value.
_WHITESPACE_ESCAPE_LETTERS = frozenset("nrtfv")
_QUOTES = frozenset("\"'")
_UNQUOTED_TERMINATORS = frozenset(",}")
#: Structural bytes where a value would START: `,`, `}` and `]` (JSON's
#: delimiters, the list's end after an empty assignment before an enclosing
#: close: `[1, "key="]`). Such a byte heads a PREFIX run (:func:`_prefix_run_end`)
#: that is NO value only when a terminator, whitespace, a quote or the text's end
#: follows the whole run; followed by a value byte the run is the value's head
#: (`key=]<secret>`, `key=,<secret>`, `key=]]<secret>`, `key=,]<secret>`), because
#: leaving it unclaimed left the secret after it standing in plaintext. Inside a
#: value `]` is a byte of it, and `,` or `}` ends it.
_NO_VALUE_OPENERS = frozenset(",}]")


def _prefix_run_end(text: str, i: int, enclosing: str) -> int:
    """The index past the PREFIX run the structural byte (:data:`_NO_VALUE_OPENERS`)
    at *i* heads: further structural bytes, a backslash paired with a structural
    byte or another backslash (the escaped encoding's inner ``\\\\`` among them),
    and the enclosing encoding's escaped whitespace between them
    (:func:`_value_head_end`). The run is read WHOLE before the value is judged:
    the base's value class admits ``]`` and a backslash, so ``key=]]<secret>``
    and ``key=,]<secret>`` are values to it, and a judgement of the first byte
    alone, which read ``]`` after ``]`` as nothing value-like, left the secret
    after the pair standing in plaintext. A backslash pair never heads a run:
    there it is the value's first byte, as the base reads it, so the caller asks
    only at a structural byte. One byte per step at least, so the run is linear
    in the text."""
    n = len(text)
    while True:
        j = i
        while j < n:
            kind, width = _inner_token(text, j, False, "", enclosing)
            if kind == "char" and width == 1 and text[j] in _NO_VALUE_OPENERS:
                j += 1
            elif kind == "backslash" and (
                width == 2 or text[j + 1] in _NO_VALUE_OPENERS or text[j + 1] == "\\"
            ):
                j += 2
            else:
                break
        j = _value_head_end(text, j, False, "", enclosing)
        if j == i:
            return i
        i = j


def _no_value_opens_at(text: str, i: int, enclosing: str) -> bool:
    """Whether NO value opens at the structural byte at *i*, where an unquoted
    value would start: past the prefix run it heads (:func:`_prefix_run_end`)
    nothing value-like follows -- whitespace, a line break, a quote, a close, or a
    bare backslash before one of those -- read as the inner token in the pair's
    encoding (`\\"` is a quote). A run reaching the text's end is the caller's
    ``pending``, decided before this is asked."""
    j = _prefix_run_end(text, i, enclosing)
    kind, _width = _inner_token(text, j, False, "", enclosing)
    if kind == "backslash":
        return text[j + 1] in _QUOTES or text[j + 1].isspace()
    return kind != "char"


#: The quote state of a LINE's prefix, as :func:`_advance_line_state` reads it:
#: the kind of the string literal open at its end (``""`` when none), whether its
#: last byte is a backslash still escaping the next one, the kind of a literal a
#: quote just closed (a doubled quote reopens it as an escaped interior quote),
#: the last byte itself, and the INNER literal open inside the outer one -- the
#: one an escaped quote delimits in the escaped encoding (``{"t":"{\\"k\\":
#: \\"v\\"}"}``): ``\\"``, ``\\'`` or ``""``. A line starts outside every literal.
_LineState = tuple[str, bool, str, str, str]
_LINE_START: _LineState = ("", False, "", "", "")

#: The line-quote state the batch pass starts its first line from. A stream
#: commits a line in pieces, so a piece can begin inside a literal whose
#: opening quote was committed before it; :class:`StreamRedactor` sets this to
#: the state at the end of what it committed before redacting the next piece,
#: and the batch pass over a whole text reads the default: a line start.
_LINE_STATE: ContextVar[_LineState] = ContextVar("_LINE_STATE", default=_LINE_START)


def _advance_line_state(state: _LineState, text: str, start: int, end: int) -> _LineState:
    """*state* advanced over ``text[start:end]``, one byte per step.

    A raw line break resets it: the literals this anchors on (a JSON string, a
    YAML or shell quoted scalar on one line, a serialized log line) do not span
    lines, so the look-back is per line. Outside a literal, a ``"`` or ``'``
    OPENS one when the byte before it is not a word byte -- an apostrophe inside
    a word (``don't``, ``O'Brien``) is prose, never an opener -- and a backslash
    escapes the byte after it. Inside a literal, the same kind of quote CLOSES
    it, a doubled quote reopens it as an escaped interior quote (YAML and SQL
    ``''``, CSV ``""``), the other kind of quote is a byte of the literal, and a
    backslash escapes the next byte -- an ESCAPED quote there delimits an inner
    literal written in the escaped encoding: it opens one, and the same escaped
    quote closes it.
    """
    kind, escaped, closed, last, inner = state
    for i in range(start, end):
        c = text[i]
        if c in "\r\n":
            kind, escaped, closed, inner = "", False, "", ""
        elif escaped:
            escaped = False
            if kind and c in _QUOTES:
                delim = "\\" + c
                if not inner:
                    inner = delim
                elif inner == delim:
                    inner = ""
        elif c == "\\":
            escaped = True
            closed = ""
        elif kind:
            if c == kind:
                kind, closed, inner = "", c, ""
        elif closed and c == closed:
            kind, closed = c, ""
        elif c in _QUOTES and not (last.isalnum() or last == "_"):
            kind, closed = c, ""
        else:
            closed = ""
        last = c
    return kind, escaped, closed, last, inner


def _enclosing_at(text: str, at: int, line_state: _LineState = _LINE_START) -> str:
    """The literals ENCLOSING position *at* of *text*, innermost last: the outer
    literal's bare quote (``"`` or ``'``) followed by the inner literal's escaped
    delimiter (``\\"`` or ``\\'``) when one is open -- ``""`` for none. Read back
    along the line from its start, or from *line_state* when the text begins
    mid-line (a stream's piece)."""
    line_start = max(text.rfind("\n", 0, at), text.rfind("\r", 0, at)) + 1
    if line_start:
        line_state = _LINE_START
    state = _advance_line_state(line_state, text, line_start, at)
    return state[0] + state[4]


def _innermost(enclosing: str) -> tuple[str, str]:
    """The innermost enclosing delimiter as written (``"``, ``'``, ``\\"``, ``\\'``
    or ``""``) and the enclosing context that is left once it closes."""
    if len(enclosing) >= 3 and enclosing[-2] == "\\":
        return enclosing[-2:], enclosing[:-2]
    return enclosing[-1:], ""


def _opener_at(text: str, at: int) -> str:
    """The quote OPENING a value at *at*, as written: bare, escaped, or none."""
    if at < len(text) and text[at] in _QUOTES:
        return text[at]
    if at + 1 < len(text) and text[at] == "\\" and text[at + 1] in _QUOTES:
        return text[at : at + 2]
    return ""


def _inner_token(
    text: str, i: int, escaped: bool, literal_quote: str, enclosing: str = ""
) -> tuple[str, int]:
    """The INNER token at *i* and its width in *text*: what one byte of the value
    reads as, in the value's encoding.

    Bare encoding (the pair stands in its own document): every token is one
    byte. Escaped encoding (the pair is written inside an enclosing string
    literal, ``key=\\"<v>\\"``): a backslash and the byte after it are ONE inner
    token, read FIRST, before any delimiter test -- ``\\\\`` the inner backslash,
    ``\\"`` the inner quote (the INNER literal's end when it is that literal's
    escaped delimiter), ``\\n`` an inner line break, ``\\t`` inner whitespace,
    ``\\/`` an inner byte -- and a BARE quote of the literal's own kind is the
    literal's end. The pair is in that encoding when its own opener is escaped
    (*escaped*) or when the look-back found it inside an enclosing literal that
    escapes with a backslash (*enclosing*, :func:`_enclosing_at`): a ``"``
    literal (JSON, a YAML or shell double-quoted scalar) or an escaped inner
    literal, whose content is escaped by construction. Inside such a literal
    every byte is in the literal's encoding whatever quote opens the value, so a
    bare ``'`` value inside ``"..."`` still reads ``\\\\`` as one backslash. A
    bare ``'`` literal (a YAML or shell single-quoted scalar) has no escapes of
    its own, so its content reads as written. A bare quote of the kind
    *enclosing* the pair is the enclosing literal's end in either encoding. A
    lone backslash at the text's end is ``partial``: its second byte has not
    arrived.

    Kinds: ``backslash``, ``quote`` (the token text carries which), ``break`` (a
    raw line break, or an escaped one), ``close`` (the enclosing literal's end),
    ``space`` (other whitespace), ``char`` (anything else), ``partial``.
    """
    c = text[i]
    if c == "\\":
        if i + 1 >= len(text):
            return "partial", 1
        if escaped or enclosing.startswith('"') or len(enclosing) >= 3:
            nxt = text[i + 1]
            if nxt in "\r\n":
                return "backslash", 1  # no encoding pairs a backslash with a raw line break
            if nxt == "\\":
                return "backslash", 2
            if nxt in _QUOTES:
                if len(enclosing) >= 3 and nxt == enclosing[-1]:
                    return "close", 2  # the inner literal's escaped delimiter
                return "quote", 2
            if nxt in "nr":
                return "break", 2
            if nxt in "tfv":
                return "space", 2
            return "char", 2
        return "backslash", 1
    if (escaped and c == literal_quote) or (enclosing and c == enclosing[0]):
        return "close", 1
    if c in "\r\n":
        return "break", 1
    if c.isspace():
        return "space", 1
    if c in _QUOTES:
        return "quote", 1
    return "char", 1


def _value_head_end(text: str, i: int, escaped: bool, literal_quote: str, enclosing: str) -> int:
    """Where the leading escaped whitespace of a value ends: a run -- of any
    length, one token per step -- of an inner backslash followed by one of
    ``_WHITESPACE_ESCAPE_LETTERS``, the way a serializer writes ``\\n<v>`` for a
    value that begins with a line break, and of the enclosing encoding's own
    escaped whitespace (``\\t`` inside a JSON string, one ``space`` token, and
    ``\\n`` there, one ``break`` token: a line break written into the string
    literal is whitespace to the anchor as a raw one is, and read as the value's
    end instead it left the whole value standing behind it). Consumed with the
    value, never a byte that ends it before it began: a cap on this run was a
    step an author who chooses a URL's encoding could take past the floor."""
    n = len(text)
    while i < n:
        kind, width = _inner_token(text, i, escaped, literal_quote, enclosing)
        if kind in ("space", "break") and width == 2:
            i += width
            continue
        if kind != "backslash" or i + width >= n:
            return i
        letter_kind, letter_width = _inner_token(text, i + width, escaped, literal_quote, enclosing)
        if letter_kind != "char" or text[i + width] not in _WHITESPACE_ESCAPE_LETTERS:
            return i
        i += width + letter_width
    return i


def _tag_run_end(text: str, i: int) -> int:
    """The index past the maximal run of WHOLE registered tags starting at *i*,
    or *i* when none starts there. A tag is one value token however many bytes
    it spans: its interior space would otherwise end an unquoted value at
    ``[REDACTED:`` and the redactor's own output would be claimed again on the
    second run. The registry's invariant (no tag is a substring of another)
    makes the parse of a run unique."""
    while True:
        for tag in CREDENTIAL_REDACTION_TAGS:
            if text.startswith(tag, i):
                i += len(tag)
                break
        else:
            return i


def scan_keyed_value(text: str, at: int, enclosing: str | None = None) -> KeyedValue:
    """Read the VALUE of a key-anchored pair whose separator ends at *at*: ONE
    explicit scanner, one token per step, for every way a value is written --
    bare or quoted, in its own document or inside an enclosing string literal.

    This is the value grammar of the three AWS key-anchored branches, the
    stream's hold, the hard URL floor, the packaging scan's standalone copy and
    the chat mirror (``sanitize.ts``), kept in one place because a regex here
    needs a patch for every new encoding -- a doubled quote, an escaped label
    quote, an escape pair, an escaped head -- and each patch has to be mirrored
    by hand. The rules, in the order they
    apply:

    * The OPENER (:func:`_opener_at`): a bare ``"``/``'``, or an escaped one
      (``\\"``) when the pair is written inside a string literal; it selects the
      value's encoding (:func:`_inner_token`) and is what an unterminated claim
      writes as its close.
    * The HEAD (:func:`_value_head_end`): leading escaped whitespace, unbounded,
      consumed with the value. A structural byte where an unquoted value would
      start heads a PREFIX run (:func:`_prefix_run_end`: structural bytes,
      backslash pairs, escaped whitespace) judged whole by the token after it: a
      value byte makes the run the value's head, anything else makes the pair
      empty, the text's end leaves it pending.
    * A run of WHOLE registered tags (:func:`_tag_run_end`) is one token
      wherever it stands in the value: the redactor's own output
      ``key=[REDACTED: credential]`` is read as the tag, not as ``[REDACTED:``
      cut at its interior space, so :func:`_value_is_credential_tag` can decide
      by byte identity whether the value IS a tag run and pass 1 can leave it
      alone -- redaction stays a fixed point of itself.
    * An UNQUOTED value runs over inner ``char`` tokens -- a byte, or in the bare
      encoding a backslash and a byte other than a quote, whitespace or a
      whitespace letter (``\\/`` in a PHP-encoded secret is the value's) -- and
      ends at whitespace, a quote, an escaped quote (the close of an enclosing
      literal), ``,`` or ``}`` (JSON's structural delimiters, so compact JSON is
      not swallowed through its closing brace), or at an escape pair carrying a
      whitespace letter (the inner line's end). A lone backslash at the text's
      end ends the value before it and leaves the scan ``pending``.
    * A QUOTED value is ONE value to its closing quote on its line: an inner
      backslash escapes the inner token after it (an escaped quote is interior),
      a DOUBLED quote is an escaped interior quote and never the close (YAML and
      SQL ``''``, CSV ``""``), the first other quote of the opener's kind closes
      it, and a raw line break -- in the escaped encoding also an escaped one or
      the enclosing literal's own bare quote -- ends the line with the value
      unterminated. A bare quote of the OTHER kind followed by a structural byte
      or whitespace reads by the opener's kind: inside a ``'``-opened value a
      ``"`` there is the close of the ``"`` string enclosing the pair and ends
      the inner line (``{"text":"key='<v>","keep":1}`` -- JSON never escapes a
      ``'`` and never writes a bare ``"`` inside a string); inside a ``"``-opened
      value a ``'`` is a byte of the value while the value's own close is still
      to come on its line (``" note' suffix"`` -- no format closes a ``"``
      string with ``'``), and the close of an enclosing ``'`` literal only when
      no ``"`` closes the value on its line; followed by a value byte it is
      interior either way (``key="it's"``), and in the escaped encoding, where
      the enclosing literal is the one that escaped the opener, always. Nothing
      after an unterminated opener is
      certified by anything, and a raw line break is where a quoted string ends
      in every format this anchors on, so the line is the widest the claim can
      be; the claim takes the line and WRITES the close. An unterminated claim
      never reaches into the next line.

    Every rule here is a STOPPING rule: a spelling the scanner does not know
    stops the claim early or runs it to the line's end, which costs an
    over-redaction and a warning, never a byte left standing in silence. Linear
    by construction: every step consumes at least one byte and looks at most two
    tokens ahead. Returns the claim as a :class:`KeyedValue`; a claim with
    ``end == start`` is a key with no value, which pass 1 does not claim (a
    presence-only reader would otherwise flag ``aws_secret_access_key=`` alone).
    """
    if enclosing is None:
        enclosing = _enclosing_at(text, at)
    n = len(text)
    opener = _opener_at(text, at)
    innermost, outer = _innermost(enclosing)
    if enclosing and opener == enclosing[0] and text[at + 1 : at + 2] == opener:
        # A DOUBLED quote of the enclosing literal's kind where the value would
        # open is that literal's escaped interior quote (a YAML single-quoted
        # scalar spells an apostrophe `''`): the value's own quote, as written,
        # and the same doubled pair closes it. Read as the literal's close and a
        # new opener it took the scalar's end with it.
        opener = opener * 2
    elif enclosing and opener in (innermost, enclosing[0]):
        # The ENCLOSING literal's own close stands where the value would open
        # (`{"template":"key=","keep":1}`, and one level down the inner
        # literal's `\"`; the outer literal's bare quote closes everything, an
        # unmatched inner `\"` before it included): the assignment inside the
        # literal is empty, and what follows the close is in the context outside
        # it -- a bare run is the value (`echo "export key="$SECRET""`), a
        # structural byte, whitespace or the line's end no value at all.
        # Reading that quote as the opener claimed the next field and broke
        # the document.
        return scan_keyed_value(text, at + len(opener), outer if opener == innermost else "")
    escaped = opener.startswith("\\")
    literal_quote = opener[-1] if opener else ""
    start = at + len(opener)
    i = _value_head_end(text, start, escaped, literal_quote, enclosing)
    if not opener:
        while i < n:
            run = _tag_run_end(text, i)
            if run > i:
                i = run
                continue
            kind, width = _inner_token(text, i, False, "", enclosing)
            if kind == "partial":
                return KeyedValue(start, i, True, "", True)
            if kind == "char" and i == start and text[i] in _NO_VALUE_OPENERS:
                # The token past the PREFIX run decides whether a value opens
                # here: nothing yet (`key=]`, `key=,]` at the text's end), and the
                # pair is pending, so the stream holds it; whitespace, a quote or a
                # close, and no value opens (an empty claim writes nothing, so a
                # second pass reads what the first did); a value byte, and the
                # run is the value's head, consumed with it.
                run = _prefix_run_end(text, i, enclosing)
                if run >= n or (text[run] == "\\" and run + 1 >= n):
                    return KeyedValue(start, i, True, "", True)
                if _no_value_opens_at(text, i, enclosing):
                    return KeyedValue(start, i, True, "", False)
                i = run
                continue
            if kind in ("space", "break", "close", "quote") or (
                kind == "char" and i > start and text[i] in _UNQUOTED_TERMINATORS
            ):
                return KeyedValue(start, i, True, "", False)
            if kind == "backslash":
                # In the escaped encoding the pair is one token, a byte of the
                # value. A BARE backslash reads the byte after it (a lone one at
                # the text's end is `partial` above): an escaped quote (an
                # out-of-view enclosing literal's close) or raw whitespace ends
                # the value; any other pair is the value's (`\/`, `\\`, `\u`, and
                # the two-byte spelling `\n` a percent-decoded URL path or a bare
                # line carries, which is no line break to the base's grammar: read
                # as one, the value stopped before it and a tag ahead of it stood
                # exempt with a secret behind). Escaped whitespace of an escaped
                # encoding arrives as one token through `_inner_token`, never here.
                nxt = text[i + 1]
                if width == 1 and (nxt in _QUOTES or nxt.isspace()):
                    return KeyedValue(start, i, True, "", False)
                i += 2
                continue
            i += width
        return KeyedValue(start, n, True, "", True)
    while i < n:
        run = _tag_run_end(text, i)
        if run > i:
            i = run
            continue
        kind, width = _inner_token(text, i, escaped, literal_quote, enclosing)
        if kind == "partial":
            return KeyedValue(start, i, False, opener, True)
        if kind == "close" and width == 1 and text[i + 1 : i + 2] == text[i]:
            # A doubled quote of the enclosing literal's kind is an escaped
            # interior quote of that literal (a YAML single-quoted scalar spells
            # an apostrophe `''`), never its close. When the value opened with
            # that doubled pair it is the value's own quote: doubled again it is
            # the value's escaped interior quote, alone it is the value's close.
            if opener == text[i] * 2:
                if text[i + 2 : i + 4] == opener:
                    i += 4
                    continue
                return KeyedValue(start, i, True, opener, False)
            i += 2
            continue
        if kind == "close" and width == 1 and i + 1 == n:
            # The quote may be the first of a doubled pair: pending until the
            # next byte says (the batch pass ends the claim here either way).
            return KeyedValue(start, i, False, opener, True)
        if kind in ("break", "close"):
            # A line break, or the enclosing literal's end: the value never
            # closed on its line.
            return KeyedValue(start, i, False, opener, False)
        if kind == "backslash":
            j = i + width
            if j >= n:
                return KeyedValue(start, i, False, opener, True)
            nxt_kind, nxt_width = _inner_token(text, j, escaped, literal_quote, enclosing)
            if nxt_kind == "break":
                i = j  # a backslash does not escape a line break, raw or inner
                continue
            if nxt_kind == "partial":
                return KeyedValue(start, j, False, opener, True)
            if nxt_kind == "close" and nxt_width == 1:
                # A BARE quote of the enclosing kind after a backslash is the
                # enclosing literal's close in every encoding. Escapes pair up at
                # the value's own depth: in the escaped encoding the backslash
                # token is the inner backslash (`\\`, a complete pair of the
                # ENCLOSING encoding), the bytes after it are unescaped in that
                # encoding, and a bare quote there is never an inner token (the
                # inner encoding writes its quotes `\"`), so it ends the inner
                # line with the value unterminated (`{"text": "\"key\": \"x\\",
                # "keep": 1}`); a `'` literal has no escapes at all, so its close
                # stands whatever byte precedes it (`text: 'key="x\'`). Read as
                # the escaped token it took the close with it and the claim ran
                # into the next field. The inner literal's escaped delimiter
                # (width 2) after a same-depth backslash is interior. A doubled
                # quote is still the enclosing literal's escaped interior quote.
                if text[j + 1 : j + 2] == text[j]:
                    i = j + 2
                    continue
                if j + 1 == n:
                    return KeyedValue(start, j, False, opener, True)
                return KeyedValue(start, j, False, opener, False)
            i = j + nxt_width  # the escaped token is interior, an enclosing quote included
            continue
        if kind == "quote" and text[i : i + width] == opener:
            j = i + width
            if j < n and text[j : j + width] == opener:
                # A doubled quote is an escaped interior quote, never the close.
                i = j + width
                continue
            return KeyedValue(start, i, True, opener, False)
        # A quote of the OTHER kind is a byte of the value: the literal that
        # could end the line here is the ENCLOSING one, and its quote reads as
        # `close` above. Nothing else closes a `"` string with `'`, or a `'`
        # string with `"`: an apostrophe before whitespace or punctuation
        # inside a `"` value is prose (`" note' suffix"`).
        i += width
    return KeyedValue(start, n, False, opener, True)


def _scan_at(text: str, match: "re.Match[str]") -> tuple[int, int, int]:
    """``(at, start, end)`` for a key-anchored *match*: where its value scan
    begins, and the branch's own value group, whose end floors the answer."""
    start, end = _credential_value_span(match)
    if start == end:
        return start, start, end
    at = start
    if start - 1 >= match.start() and text[start - 1] in _QUOTES:
        at = start - 2 if start - 2 >= match.start() and text[start - 2] == "\\" else start - 1
    return at, start, end


def _keyed_value_of(
    text: str, match: "re.Match[str]", enclosing: str | None = None
) -> KeyedValue | None:
    """The value claim of a key-anchored *match* of ``_CREDENTIAL_PATTERNS``, or
    ``None`` for a whole-match branch.

    The three AWS branches end at their separator with an EMPTY value group
    marking where the value begins, and the scanner reads the opener and the
    value from there. The Bearer branch keeps its own value group (``Bearer
    <b64token>``, a scheme and a token with whitespace between them, which an
    unquoted scan would end at); its opener is the quote the branch consumed
    right before the group, and the group's own end is the floor the scan's
    answer never shrinks below. *enclosing* is the literal the look-back found
    the key inside (:func:`_enclosing_at`), read here when not given.
    """
    if match.lastindex is None:
        return None
    at, start, end = _scan_at(text, match)
    value = scan_keyed_value(text, at, enclosing)
    if start != end and value.end < end:
        value = value._replace(end=end, closes=True, pending=False)
    return value


class _KeyedValueScans:
    """One traversal's reader of key-anchored values -- :func:`_keyed_value_of`
    with the LINE's quote state carried from match to match, and the last
    UNQUOTED scan remembered, so a traversal reads every byte once.

    The look-back that tells the scanner which literal encloses a key
    (:func:`_enclosing_at`) walks the line from its start; a traversal that ran
    it afresh for every anchor would read a line of many anchors as many times
    as it has anchors. The matches of one text arrive in order, so the state
    machine is advanced from the last anchor to the next and the walk is one
    pass. A stream's piece can begin inside a literal opened in the piece
    committed before it: :class:`StreamRedactor` passes the state at the end of
    what it committed as the first line's start, and ``_LINE_STATE`` carries it
    to the batch pass over the piece.

    A bare scan carries no state but its position: it ends at the first
    terminator after its start, and a key-anchored match that begins inside the
    run it read -- ``SecretAccessKey=SecretAccessKey=...``, a key repeated as
    its own value -- starts its own bare scan at a token boundary of that run
    (its separator is one ``char`` token or the second byte of an escape pair,
    and no registered tag carries a key), so it reads the same tokens to the
    same end, with the same ``pending``; a run holds no quote, so the enclosing
    literal is the same at every anchor inside it. A quoted opener inside a bare
    run is impossible, since a quote ends the bare scan, so the memo is never
    asked for a quoted value; the Bearer branch, whose own group floors its
    answer, is read without it. Scanning each nested match afresh cost the run's
    length per match, and a watched file of 5 000 repeated keys (80 KB) spent
    73 s in pass 1 -- past the 25 s watchdog budget of the loop that runs it.
    The answer is the one the fresh scan gives, byte for byte (the fixture's
    ``nested-*`` rows), and the work is one scan per run.
    """

    __slots__ = ("_bare", "_state", "_pos")

    def __init__(self, line_state: _LineState | None = None) -> None:
        self._bare: KeyedValue | None = None
        self._state = _LINE_STATE.get() if line_state is None else line_state
        self._pos = 0

    def enclosing_at(self, text: str, at: int) -> str:
        """The literal enclosing *at*, with the line walked from the last anchor."""
        if at < self._pos:
            # Out of order: start the line over (a line start is outside every
            # literal; the carried state belongs to the text's first line only).
            return _enclosing_at(text, at, self._state if "\n" not in text[:at] else _LINE_START)
        self._state = _advance_line_state(self._state, text, self._pos, at)
        self._pos = at
        return self._state[0] + self._state[4]

    def value_of(self, text: str, match: "re.Match[str]") -> KeyedValue | None:
        if match.lastindex is None:
            return None
        at, start, end = _scan_at(text, match)
        enclosing = self.enclosing_at(text, at)
        if start != end:
            return _keyed_value_of(text, match, enclosing)
        bare = self._bare
        if bare is not None and bare.start <= start < bare.end and not _opener_at(text, start):
            return KeyedValue(start, bare.end, True, "", bare.pending)
        value = scan_keyed_value(text, start, enclosing)
        if not value.opener:
            self._bare = value
        return value


_LONGEST_TAG = max(len(tag) for tag in CREDENTIAL_REDACTION_TAGS)


def _value_ends_in_a_tag_prefix(text: str, value: KeyedValue) -> bool:
    """Whether *text* ends in a strict prefix of a registered tag that begins
    INSIDE *value* (at its start, after whole tags, or after glued bytes).

    The scanner reads a whole tag as one token wherever it stands, but a tag cut
    off by the end of the buffer is read byte by byte and its interior space
    ends an unquoted value, so the value reads as terminated while its last
    token may still be arriving. Only the buffer's last ``_LONGEST_TAG - 1``
    bytes can hold such a prefix, so the test is a bounded scan per match and
    the hold stays linear in the buffer."""
    end = len(text)
    i = text.find("[", max(value.start, end - _LONGEST_TAG + 1), value.end)
    while i != -1:
        tail = text[i:]
        if any(len(tail) < len(tag) and tag.startswith(tail) for tag in CREDENTIAL_REDACTION_TAGS):
            return True
        i = text.find("[", i + 1, value.end)
    return False


def _key_anchored_hold_start(
    text: str, cut: int, line_state: _LineState | None = None
) -> int | None:
    """Where a stream must hold *text* from so a key-anchored pair reaches the
    batch pass WHOLE when it would otherwise commit at *cut*, or ``None`` when
    that cut bisects no pair.

    The stream commits up to the last byte outside its credential class, and a
    key-anchored pair has places where such a byte sits INSIDE the pair: the
    whitespace after the separator (``"aws_secret_access_key": `` -- a committed
    label leaves the value to arrive anchor-less in the next window and stream
    raw, as it did on every head before this one) and a space or a backslash
    inside a quoted value (``key="<v> tail"`` -- the batch pass over a head cut
    at the space claims to the head's end and WRITES the close there, so the
    wire reads ``key="[REDACTED: credential]"tail"``). So a cut that lands
    inside a pair's extent is pulled back to the pair's start: the extent is
    the label through the value as :func:`scan_keyed_value` reads it, through
    the closing quote when the value is quoted, and to the text's end while the
    scan is ``pending`` -- a quote still open, an unquoted run with no
    terminator yet, an opener or a lone backslash with nothing after it; a
    label cut short before its separator (``_KEY_ANCHORED_LABEL_TAIL_RE``) is a
    pair whose value is still to come. A closing quote followed
    by a terminator, a raw line break, or the stream's end releases the hold --
    at the stream's end the batch pass is right to write the close. The hold is
    WEAK in the stream's terms: it never raises the hold-back cap and never
    authorizes a drop, and the stream drops a hold whose extent would exceed the
    cap rather than flooring it -- the floor cuts wherever ``len - cap`` lands, a
    token's run included, while the natural cut never bisects a credential-class
    run -- so a quoted value that runs past the cap commits its label and token
    whole and only its prose tail takes the close written at the cut.
    """
    if not any(
        key in text for key in _KEY_ANCHORED_KEYS
    ) and not _CREDENTIAL_PREFILTER_AUTHORIZATION_RE.search(text):
        return None
    held: int | None = None
    label = _KEY_ANCHORED_LABEL_TAIL_RE.search(text)
    if label is not None and label.start() < cut:
        held = label.start()
    scans = _KeyedValueScans(line_state)
    extents: list[tuple[int, int]] = []
    for match in _credential_matches(text):
        if match.start() >= cut:
            break
        value = scans.value_of(text, match)
        if value is None:
            continue
        if not value.pending and _value_ends_in_a_tag_prefix(text, value):
            # The value ended at the buffer's tail INSIDE a strict prefix of a
            # registered tag (`key=[REDACTED: ` -- the tag's interior space is
            # a terminator to the unquoted scan), wherever in the value that
            # prefix begins: after whole tags (`key=<tag>[REDACTED: `) or glued
            # bytes as much as at its start. The tag may complete in the next
            # chunk, with bytes glued to it that the batch pass must see with
            # their key; committing the label here streamed those bytes
            # anchor-less and raw (the fixture's `tag-glued` rows,
            # `test_a_value_ending_in_a_cut_off_tag_after_whole_tags_is_held_from_its_key`).
            value = value._replace(pending=True)
        if value.pending:
            # The value (or its opener, or an escape's second byte) is still to
            # come: a cut AT the text's end bisects the pair as surely as one
            # inside it.
            pair_end = len(text) + 1
        elif value.closes and value.opener:
            pair_end = value.end + len(value.opener)
        else:
            # Unquoted and terminated, or a line ended the quoted value: the
            # pair's extent is known.
            pair_end = value.end
        extents.append((match.start(), pair_end))
        if cut < pair_end:
            held = match.start() if held is None else min(held, match.start())
    # The pulled-back cut is judged again: pulling it to one pair's start can
    # land it inside an EARLIER pair's extent -- a label still waiting for its
    # separator (`aws_access_key_id = \naws_session_token `) begins where the
    # first pair's value does, since the separator's whitespace crossed the
    # line break -- and a cut there commits the first pair's label without the
    # value the batch pass claims for it. Each pass moves the cut strictly
    # left, so one walk back over the pairs settles it (the fixture's
    # `empty-then-key-*` rows: every chunk size equals the batch pass).
    if held is not None:
        for start, pair_end in reversed(extents):
            if start < held < pair_end:
                held = start
    return held


def _keyed_value_group(class_: str) -> str:
    """The body of a key-anchored VALUE group over one-character class *class_*.

    ``TAG<class>*|<class>+``: see ``_CREDENTIAL_TAG_ATOM``. Non-empty on both
    alternatives, so a key with no value never matches (a presence-only consumer
    would otherwise start flagging the bare key alone). Pass 4's ``token=``
    parameter value is the one grammar still spelled this way; the three AWS
    key-anchored branches end at their separator and hand the value to
    :func:`scan_keyed_value`.
    """
    return f"{_CREDENTIAL_TAG_ATOM}{class_}*|{class_}+"


def _value_is_credential_tag(text: str, start: int, end: int) -> bool:
    """Whether the value at ``text[start:end]`` IS one of this module's own tags
    -- the one value a value-redacting pass declines to claim.

    Several surfaces run the redactor over its own output (the streaming path
    re-redacts the persisted copy; :func:`redact_path_segments` requires its
    candidate to be a fixed point), so a value that is a tag must stay a tag:
    claiming it again would mangle ``key=[REDACTED: credential]`` into
    ``key=[REDACTED: credential] credential]`` on the second run. Pass 1 (a
    key-anchored branch's value group) and pass 4 (a ``token=`` parameter value)
    share this one rule, and both embed ``_CREDENTIAL_TAG_ATOM`` in their value
    group so the value they hand here is the whole tag, not its head.

    Trust is BYTE IDENTITY of the ENTIRE value with a module-owned fixed literal,
    never a shape and never a prefix: ``[``, ``]`` and ``:`` are ordinary value
    bytes, so ``[REDACTED<secret>`` and ``[redacted:<secret>`` are values and are
    redacted like any other, and ``[REDACTED: credential]<secret>`` -- a tag with
    bytes glued to it -- is a value too and is redacted whole with a warning,
    because a consumer that gates egress on the warning list (``decisions.gate``)
    must not be told a key-anchored line was clean when uncertified bytes rode
    behind its tag. ``CREDENTIAL_REDACTION_TAGS`` is the registry because it
    holds ONLY fixed literals; the exfiltration tag's domain segment is
    attacker-satisfiable and is deliberately not trusted.

    A RUN of whole tags is a tag too: two credentials adjacent inside one value
    (``?token=AKIA…AKIA…``) are two claims, and the pass that wrote them --
    including the one this module shipped before pass 4 coalesced such a value
    -- left ``[REDACTED: credential][REDACTED: credential]`` standing. Reading
    that run as a value would claim its head up to the interior space and mangle
    it on the next run, with a warning about text that holds no secret. The
    registry's invariant (no tag is a substring of another) makes the parse of a
    run unique, and a run with bytes glued to its last ``]`` is, as above, a value.
    """
    i = start
    while i < end:
        for tag in CREDENTIAL_REDACTION_TAGS:
            if i + len(tag) <= end and text.startswith(tag, i):
                i += len(tag)
                break
        else:
            return False
    return end > start


# ── Credential Output Redaction ──
# Catches raw credential patterns in LLM output / tool results,
# including base64-encoded variants.  Applied on all output paths
# alongside redact_exfiltration_urls().
#
# ⚠ THIS PATTERN HAS A DEPENDENT PRE-FILTER. `_might_contain_credential` below
# gates the scan of this pattern on a cheap necessary condition, and
# `redact_credentials` SKIPS the scan entirely when that gate returns False. The
# gate is therefore part of the redaction boundary, not an optimisation detail:
# any input a branch here accepts but the gate rejects is a silent leak.
#
# So EDITING A BRANCH IS A TWO-SITE CHANGE:
#   * ADDING a branch     -> register a sample in `test_credential_prefilter.py`
#                            and an anchor in `_might_contain_credential`.
#                            `test_every_pattern_branch_has_a_prefilter_anchor`
#                            fails on the branch count until you do.
#   * WIDENING a branch   -> widen the corresponding anchor to match, because the
#                            anchor must stay a SUPERSET of the branch. A widened
#                            branch does NOT change the branch count, so the count
#                            assertion cannot see it. Two tests cover this:
#                            `test_a_widened_branch_cannot_outgrow_its_anchor`
#                            enumerates each branch's own alternatives, so a NEW
#                            alternative (a second token prefix) is caught; and
#                            `test_widening_a_branch_cannot_outgrow_its_anchor`
#                            perturbs each sample, so a case-fold or homoglyph
#                            relaxation is caught.
#   * Making a branch CASE-INSENSITIVE -> the anchor MUST use the same regex
#                            engine. A case-sensitive literal cannot gate a
#                            `(?i:…)` branch, and neither can `str.lower()` —
#                            see `_CREDENTIAL_PREFILTER_AUTHORIZATION_RE` for the
#                            bypass that cost.
#
# AND A BRANCH DECIDES ITS OWN REDACTION SPAN BY ITS GROUP SHAPE. A branch that
# starts at the KEY naming a secret (`aws_secret_access_key = <v>`,
# `Authorization: Bearer <v>`) wraps the VALUE in one named capturing group, and
# `_credential_value_span` redacts that group alone: the key, the `:`/`=`
# separator and the quotes around the value survive, so a JSON / YAML / INI /
# `.env` / header-line document keeps its structure and JSON still parses.
# Replacing the whole match there collapses `"Authorization": "Bearer <v>"` to
# one bare string, and a file viewer then reports a valid file as invalid JSON.
# A branch with NO capturing group IS the secret (`AKIA…`, a PEM block, a
# fixed-prefix token, `scheme://user:pass@`) and is replaced whole. The two
# shapes are therefore mutually exclusive: a capturing group on a whole-match
# branch would narrow its span and leak the rest of the token, and a key-anchored
# branch without one collapses the pair again. Capturing groups here are NAMED
# and appear nowhere but as the value of a key-anchored branch;
# `test_redaction_key_anchored_value_span.py` pins both directions. And because
# the key now survives, a re-run over the output meets `key = [REDACTED: …]`
# again: pass 1 declines a value that is one of this module's own tag literals
# (`_value_is_credential_tag`), so redaction stays a fixed point of itself.
_CREDENTIAL_PATTERNS = re.compile(
    r"(?:"
    # ── AWS ──
    f"{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    # key-value forms: tolerate an optional closing quote after the key name and an
    # optional opening quote before the value so JSON (`"aws_secret_access_key": "v"`)
    # is redacted, not just bare `key=v` / `key: v`. Without the `["']?` the closing
    # quote in JSON sits between the key and `:` and defeats the match → secret leaks.
    # The three branches END at their separator: the named group is EMPTY and
    # marks where the value begins, and `scan_keyed_value` reads the opener and
    # the value from there -- one explicit scanner instead of a value class (a
    # regex here was patched for a new encoding on four consecutive review
    # rounds, and every patch had to be mirrored by hand). The key, its closing
    # quote (`_LABEL_QUOTE`: bare, or escaped as it reads inside an enclosing
    # string literal) and the separator are matched but not redacted (see
    # `_credential_value_span`); a key with no value is a claim of zero bytes,
    # which pass 1 declines.
    r"|(?:SecretAccessKey|aws_secret_access_key)"
    + _LABEL_QUOTE
    + r"\s*[:=]\s*"
    + "(?P<aws_secret_value>)"
    r"|(?:SessionToken|aws_session_token)"
    + _LABEL_QUOTE
    + r"\s*[:=]\s*"
    + "(?P<aws_session_value>)"
    r"|(?:AccessKeyId|aws_access_key_id)" + _LABEL_QUOTE + r"\s*[:=]\s*" + "(?P<aws_key_id_value>)"
    # PEM private key: match the ENTIRE block (header + base64 body), not just
    # the header phrase. redact_credentials() replaces the matched SPAN, so a
    # header-only match (the original form) left the secret base64 body verbatim.
    # Two mutually exclusive tails after the header:
    #   1. Full block — ``[\s\S]*?`` (any char, incl. newlines) spans the body
    #      lazily to the first END marker. ``[\s\S]`` (not a base64 char class)
    #      is required so encrypted keys — whose ``Proc-Type:``/``DEK-Info:``
    #      headers carry ``:`` and ``,`` — are fully spanned rather than cut
    #      short at the first non-base64 char.
    #   2. Truncated block (no END) — consume only *subsequent* PEM body lines:
    #      each continuation must start with a newline and be a base64 line or a
    #      ``Proc-Type:``/``DEK-Info:`` metadata header. This deliberately does
    #      NOT use ``$``/``\Z``: without re.MULTILINE ``$`` means end-of-STRING,
    #      so a lazy ``[\s\S]*?`` with a ``|$`` fallback swallowed everything
    #      from a header mentioned inline in prose (LLM output, docs) to the end
    #      of the string — silently deleting all trailing lines. Requiring a
    #      leading newline per line means an inline header in prose (real key
    #      material always begins on the line *after* the header) matches only
    #      the header phrase, leaving trailing content intact, while a genuine
    #      truncated key still has its body lines redacted.
    #      The final ``(?=\r?\n[A-Za-z0-9+/=])`` lookahead alternative lets the
    #      run cross a SINGLE blank line when the *next* line begins with base64
    #      material. RFC 1421 ENCRYPTED PEMs put a MANDATORY blank line between
    #      the ``DEK-Info:`` header and the base64 body; without this lookahead
    #      the per-line "every continuation must contain a base64 char" rule
    #      stopped at that blank line and leaked the whole encrypted body (for
    #      both a truncated key AND a complete encrypted key whose body exceeds
    #      the full-block cap). Because the lookahead consumes nothing, TWO+
    #      consecutive blank lines still terminate the run — trailing prose is
    #      preserved (no over-redaction).
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:"
    r"[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?:\r?\n(?:Proc-Type:[^\n]*|DEK-Info:[^\n]*|[A-Za-z0-9+/=]+(?=\r?\n|\Z)"
    r"|(?=\r?\n[A-Za-z0-9+/=])))*"
    r")"
    r"|xox[bpas]-[0-9a-zA-Z-]{10,}"  # Slack token
    # Telegram bot token: ``<bot_id>:<secret>`` — bot_id is 6+ digits, secret is
    # ~35 URL-safe base64 chars. The ``{30,}`` floor sits deliberately below the
    # real length so shortened/rotated test tokens are still caught. Analogue to
    # the Slack token above. Telegram tokens can live in ``config.json``
    # (agent-readable), so an echoed config would otherwise leak a full
    # bot-control credential unredacted. The value class ``[A-Za-z0-9_-]`` stops
    # at structural delimiters (space, quote, comma, brace), so it can't swallow
    # adjacent fields; over-redacting a rare ``digits:token`` lookalike is the
    # safe direction.
    r"|[0-9]{6,}:[A-Za-z0-9_-]{30,}"  # Telegram bot token
    # Discord bot token: three base64url segments — ``base64(application_id)``,
    # a 6-char timestamp, and an HMAC. The first segment is base64 of a decimal
    # snowflake, so its leading character is fixed by the id's first digit
    # (``M``/``N``/``O`` for the 1-9 range every live snowflake starts with), and
    # the timestamp segment is always EXACTLY 6 characters. Both anchors matter:
    # the same rule written as three open-ended runs matches an ordinary dotted
    # identifier or a base64 blob with periods in it, and a redactor that eats
    # arbitrary text is a different bug. Length floors sit below the real ones so
    # a shortened/rotated test token is still caught. Same reasoning as Telegram
    # above — ``discord.bot_token`` can live in ``config.json``, which the agent
    # can read, so an echoed config would otherwise leak bot control verbatim.
    # The boundary guards keep the leading ``[MNO]`` from landing mid-run inside
    # a longer base64 blob and redacting an arbitrary tail of it, the same way
    # the link-token branch below guards its own ``eyJ`` anchor.
    r"|(?<![A-Za-z0-9_-])[MNO][A-Za-z0-9_-]{22,30}"
    r"\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}(?![A-Za-z0-9_-])"  # Discord bot token
    # ── Third-party developer credentials ──
    # Distinctive, fixed-case prefixes → very low false-positive risk.  Minimum
    # lengths are kept slightly below the real token lengths so shortened test /
    # rotated variants are still redacted (over-redaction on a prefix match is the
    # safe direction).  Case-sensitive by design (these prefixes are issued in a
    # fixed case); do NOT fold — folding would broaden false positives.
    r"|gh[opsur]_[A-Za-z0-9]{30,255}"  # GitHub PAT (ghp_) + oauth/user/server/refresh
    r"|github_pat_[A-Za-z0-9_]{40,}"  # GitHub fine-grained PAT
    r"|glpat-[A-Za-z0-9_-]{16,}"  # GitLab PAT
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"  # Stripe secret / restricted keys
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"  # SendGrid API key
    r"|sk-proj-[A-Za-z0-9_-]{16,}"  # OpenAI project key
    r"|sk-ant-[A-Za-z0-9_-]{16,}"  # Anthropic API key
    r"|npm_[A-Za-z0-9]{24,}"  # npm access token
    r"|pypi-[A-Za-z0-9_-]{16,}"  # PyPI API token
    r"|do[opr]_v1_[A-Za-z0-9]{40,}"  # DigitalOcean PAT/OAuth/refresh
    r"|GOCSPX-[A-Za-z0-9_-]{20,}"  # Google OAuth client secret
    # Connection/fetch URIs with embedded credentials — redact the
    # ``scheme://user:pass@`` prefix (the password lives here). http(s)/ftp(s)
    # are included because URL userinfo is a credential wherever it appears
    # (e.g. a token-bearing artifact CDN base quoted by an update-failure
    # message); the user:pass@ shape cannot false-positive on a bare URL — a
    # port (``:8080``) is never followed by ``@`` within the authority.
    r"|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|amqp(?:s)?"
    r"|https?|ftps?)"
    # User portion is `*` (not `+`): an empty-user string (`mongodb+srv://:secret@…`) still redacts.
    # Password segment allows ``@`` (``[^\s/]`` not ``[^\s/@]``): an unencoded
    # ``@`` inside a password is common, and stopping the match at the FIRST
    # ``@`` would redact only the head and leak the rest (``…ss@host``) to
    # logs. ``/`` still bounds the authority, so greedy ``+`` consumes through
    # the FINAL ``@`` — the real userinfo/host separator — and never past it.
    r"://[^\s:/@]*:[^\s/]+@"
    # ── JWT / JWE / OAuth Bearer tokens ──
    # `eyJ` is the base64url encoding of every JWT header's `{"` prefix; a signed
    # JWT (JWS) is three `.`-separated base64url segments (header.payload.sig), an
    # encrypted JWT (JWE, RFC 7516) is five (header.key.iv.ciphertext.tag), and our
    # OWN dashboard link token is two — `base64url(payload).base64url(hmac_sig)`,
    # see `dashboard.token_auth.generate_token`. The 3-and-5-segment shapes are
    # matched by the `{2,4}` quantifier below; the 2-segment link token has its
    # OWN separately bounded alternative.
    #
    # The floor stays at 2 because the two-segment dashboard token is what a higher
    # floor drops: it would not match here at all and would fall through to the
    # bare-secret entropy pass, whose run class `[A-Za-z0-9+/]` is STANDARD base64
    # and excludes base64url's `-`/`_`. That makes redaction depend on which
    # characters a random HMAC signature happens to contain. That rate is derivable,
    # so it is stated as a closed form rather than as a sample. HMAC-SHA256 is 256
    # bits and base64url-unpadded gives 43 chars. The first 42 each carry a full 6
    # bits, so each is uniform over the 64-char alphabet, of which exactly 2 are
    # `-`/`_`. The 43rd carries only the leftover 4 bits (256 - 42*6), and they
    # land in the HIGH bits of its 6-bit
    # group with the low 2 bits zero, so it spans exactly the 16 alphabet indices
    # divisible by 4 (`048AEIMQUYcgkosw`) and can never be `-`/`_`, which sit at
    # 62/63. Hence P(no `-`/`_`) = (62/64)^42 = 26.4%, verified by encoding all
    # 256 possible final digest bytes.
    # So roughly a quarter of tokens would have only the signature replaced (leaving
    # the payload claims verbatim in a URL that still looks complete but is not
    # authenticated), and the other ~74% would stream out entirely unredacted.
    # Matching the whole token here makes the outcome deterministic and replaces it
    # as one unit. The 2-segment token gets its OWN alternative rather than
    # relaxing the segment floor to `{1,4}`. Relaxing
    # the floor over-redacts ordinary code and prose, because the pattern has no left
    # boundary and post-header segments allow an EMPTY match: `keyJson.get(raw)` then
    # redacts to `k[REDACTED…](raw)`, and a JWT quoted at the end of a sentence loses
    # its trailing period. The 3-to-5-segment alternative (`credential_patterns.JWT_MULTI_SEGMENT`,
    # `*` on post-header segments so an empty JWE segment matches) consumes such a period too, an
    # accepted over-redaction cost. The 2-segment alternative therefore carries a left boundary
    # (`(?<![A-Za-z0-9_.-])`: base64url's `-`/`_` plus `.` so `obj.eyJ…` is excluded too;
    # `_BARE_SECRET_RUN_RE` uses `(?<![A-Za-z0-9+/])`) and per-segment lengths taken from
    # the generator, not from guesswork, because a length FLOOR alone is beatable by a
    # sufficiently verbose identifier: at `{40,}` the 40-char
    # `eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue` matched.
    #
    # `token_auth._sign` is HMAC-SHA256 base64url-unpadded, so the signature is
    # EXACTLY 43 chars for every token ever minted; that is a property of the digest,
    # not of the payload, so it is pinned as `{43}` rather than a floor. See
    # `test_link_token_signature_is_43_chars`, which fails loudly if `_sign` changes
    # digest, instead of letting redaction silently stop matching.
    #
    # `generate_token` always emits 6 claims (`sub`/`exp`/`session_exp`/`iat`/`nonce`/
    # `gen`), with a 16-hex-char nonce and float timestamps; `app`, `prompt` and
    # `extra` only ADD. Payload length is NOT fixed. It scales with `len(sub)`, and
    # `json.dumps` writes each float timestamp at its own repr width, which base64
    # then quantises into 4-char steps. So the floor is derived, not sampled: a
    # 1-char `sub` (the narrowest a caller passes: the app validator requires at
    # least one char and the other call sites supply a literal fallback), `gen=0`,
    # and all three timestamps at their shortest 12-char repr (an exactly-integral
    # `time.time()` in the current 10-digit epoch era) measures 145 chars past
    # `eyJ`, which leaves the `{96,}` floor 49 chars of headroom against a future
    # shorter claim set while still excluding `eyJ2IjoxfQ.json`. ONLY that derived
    # floor is pinned, by `test_link_token_payload_clears_the_96_char_floor`, which
    # reads the bound from the compiled pattern and the claim keys from a real mint
    # so a dropped claim fails loudly instead of silently disabling redaction. Live
    # payloads are much larger and are NOT pinned, because the exact spread moves
    # with float reprs and caller mix: measured 168-185 for the mandatory-only
    # callers and 192-223 for the two that also pass `app=` (`handlers/core.py`,
    # `token_auth.py`), which adds an `"app"` claim.
    #
    # Order matters: the 3-to-5-segment
    # alternative is tried first at each position, so a real JWS still redacts whole
    # instead of matching `header.payload` and leaving `.signature` exposed.
    # The 3-to-5-segment alternative keeps `*` (not `+`) on post-header segments so an
    # EMPTY segment still counts: a compact JWE with direct
    # (`alg:dir`) or key-agreement (`ECDH-ES`) key management has an empty Encrypted
    # Key (2nd) segment — shape `header..iv.ciphertext.tag` — which a `+` quantifier
    # would fail to match, leaking the ciphertext + tag.
    # The HTTP `Authorization: Bearer <token>` header carries opaque or JWT bearer
    # creds. The JWT alternative is case-sensitive (`eyJ` is a fixed base64url
    # prefix). The header name + scheme are matched case-insensitively via scoped
    # `(?i:…)` groups because HTTP header names are case-insensitive (RFC 7230
    # §3.2), HTTP/2 mandates lowercase names, and the `Bearer` scheme is
    # case-insensitive (RFC 6750 §2.1) — so `authorization: bearer …` emitted by
    # requests / net/http / HTTP2 frame logs is redacted too. The separator is
    # JSON-aware: an optional quote may precede the
    # `:`/`=` and the token, so a serialized header `{"Authorization": "Bearer
    # <tok>"}` in a structured-log/JSON request dump is redacted as well. Both
    # alternatives are scoped tightly: the JWT segment class cannot cross the
    # literal `.` separators and the Bearer token class (`[A-Za-z0-9._~+/-]`, RFC
    # 6750 `b64token`) stops at whitespace/quotes, so neither over-captures. A
    # Bearer header carrying a JWT redacts as one match (the Bearer class subsumes
    # the JWT); a bare JWT is still caught independently (defense in depth).
    # The redacted VALUE is `Bearer <token>` — the header's credentials (RFC 6750
    # §2.1: `credentials = "Bearer" 1*SP b64token`) — so the scheme goes with the
    # token and `"Authorization": "Bearer <tok>"` reads back as
    # `"Authorization": "[REDACTED: credential]"`: the header name, the separator
    # and the quotes survive, and the document still parses.
    f"|{JWT_MULTI_SEGMENT}"  # JWS (3-seg) / JWE (5-seg incl. dir/ECDH-ES), shared spelling
    r"|(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]{96,}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"  # 2-seg link token
    r"|(?i:Authorization)"
    + _LABEL_QUOTE
    + r"\s*[:=]\s*"
    + _LABEL_QUOTE
    + r"(?P<bearer_value>(?i:Bearer)\s+[A-Za-z0-9._~+/-]+=*)"  # HTTP/JSON bearer
    r")",
)


def get_credential_patterns() -> list[re.Pattern[str]]:
    """Public accessor for the canonical credential regexes.

    Lets other modules (e.g. deploy-web's pre-publish content scan) reuse the
    same patterns without coupling to the private ``_CREDENTIAL_PATTERNS`` name,
    so a future rename here can't silently turn a downstream scan into a no-op.
    Returns a list so callers can iterate uniformly; the fork keeps a single
    combined compiled regex, so the list has one element.

    These are the RAW patterns: a key-anchored branch matches the redactor's own
    output (``key=[REDACTED: credential]`` -- the key survives redaction) exactly
    as it matches the secret it replaced. A reader asking whether text CARRIES a
    credential uses :func:`contains_credential` or :func:`credential_matches`,
    which apply pass 1's own skip for a tag standing as the value.
    """
    return [_CREDENTIAL_PATTERNS]


# The JWS/JWE branch is shape-only (it matches `honeyJar.example.com`), so its hits need a
# JSON-object header. Only it and the one-dot link token start with `eyJ`: two dots mark them.
_CREDENTIAL_PATTERNS_SANS_JWT = re.compile(
    _CREDENTIAL_PATTERNS.pattern.replace(f"|{JWT_MULTI_SEGMENT}", "", 1)
)


def _is_json_object_segment(segment: str) -> bool:
    """Whether *segment* base64url-decodes to a JSON object: JOSE, itsdangerous, Flask session."""
    try:
        header = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, RecursionError):
        return False
    return isinstance(header, dict)


def _credential_matches(text: str) -> Iterator[re.Match[str]]:
    """``_CREDENTIAL_PATTERNS.finditer(text)``, minus JWT-branch hits whose header is not JSON.

    A rejected hit retries the other branches at its start, then resumes one character on,
    so a credential nested inside the rejected span is still found. A header holding a
    second ``eyJ`` stays a credential: rejecting it would rescan that header once per ``eyJ``.
    """
    pos = 0
    while (m := _CREDENTIAL_PATTERNS.search(text, pos)) is not None:
        header = m.group().split(".", 1)[0]
        if (
            m.group().startswith("eyJ")
            and m.group().count(".") >= 2
            and header.find("eyJ", 1) == -1
            and not _is_json_object_segment(header)
        ):
            alt = _CREDENTIAL_PATTERNS_SANS_JWT.match(text, m.start())
            if alt is None:
                pos = m.start() + 1
                continue
            m = alt
        yield m
        pos = max(m.end(), m.start() + 1)


def _match_is_live_credential(
    text: str, match: "re.Match[str]", scans: "_KeyedValueScans | None" = None
) -> bool:
    """Whether pass 1 claims or warns on *match* -- the one question a reader
    that only asks "does the pattern match?" needs answered instead.

    False for exactly the match pass 1 passes over in silence: a key-anchored
    branch whose value is a registered tag, or a run of them, that demonstrably
    FILLS its value -- unquoted, or quoted with the closing quote on its line.
    The key survives redaction, so the redactor's own output
    ``key=[REDACTED: credential]`` matches its key-anchored branch again, and a
    presence-only reader of ``_CREDENTIAL_PATTERNS`` (the ledger push gate, the
    deploy and preview scans, the exfil request gates) would otherwise refuse
    text that holds no secret -- for the ledger, every push after the first
    redacted entry. The rule is pass 1's own, judged on the same extended span,
    so the two never disagree: a tag heading a quoted line whose quote never
    closes is warned there and is live here. A loop over one text's matches
    passes its :class:`_KeyedValueScans` so a match inside the last unquoted run
    is answered without reading the run again.
    """
    value = _keyed_value_of(text, match) if scans is None else scans.value_of(text, match)
    if value is None:
        return True
    if value.end <= value.start:
        return False
    if _value_is_credential_tag(text, value.start, value.end):
        return not value.closes
    return True


def credential_matches(text: str) -> Iterator["re.Match[str]"]:
    """Pass 1's LIVE matches in *text*, in order: what :func:`redact_credentials`
    would claim or warn on.

    ``_CREDENTIAL_PATTERNS`` hits minus the two kinds pass 1 itself declines -- a
    JWT-shaped run whose header is not a JSON object (:func:`_credential_matches`)
    and a key-anchored match whose value is one of this module's own tags filling
    its value (:func:`_match_is_live_credential`). A reader that reports per match
    (a line number, a masked snippet) iterates this; a reader that needs a boolean
    calls :func:`contains_credential`. Pass 1 only, like the raw patterns it
    replaces: base64-encoded credentials stay with :func:`_contains_fixed_credential`
    and the bare-entropy heuristic with :func:`_text_contains_bare_secret`.
    """
    # A fresh line start, never the carried stream state: the text judged here is
    # its own document (a base64-decoded chunk, a reader's text), not the raw
    # stream piece pass 1 scans with the state the pieces before it left. Read
    # with that state, a decoded `key="[REDACTED: credential]` took the raw
    # piece's open quote as its enclosing literal and the tag-filled value as
    # closed and exempt, and the encoded secret passed the stream.
    scans = _KeyedValueScans(_LINE_START)
    for match in _credential_matches(text):
        if _match_is_live_credential(text, match, scans):
            yield match


def contains_credential(text: str) -> bool:
    """Presence-only companion to :func:`redact_credentials`: whether pass 1
    would claim or warn on *text*.

    The raw :func:`get_credential_patterns` ``search`` reads the redactor's own
    ``key=[REDACTED: credential]`` as a live credential; this does not, and is the
    accessor a presence-only reader uses. See :func:`credential_matches`.
    """
    return next(credential_matches(text), None) is not None


def _contains_credential_pattern(text: str) -> bool:
    """Validated, tag-aware ``_CREDENTIAL_PATTERNS.search``: see :func:`credential_matches`."""
    return contains_credential(text)


# ── Cheap pre-filter for `_CREDENTIAL_PATTERNS` (performance only) ──
# `_CREDENTIAL_PATTERNS` is a 23-branch alternation, so `re` retries every branch
# at essentially every position: measured 117 ns/char, and it is the single
# hottest line in the gateway's event loop (38.2% of all py-spy samples, reached
# per message per dirty-slot flush). The scan cost is paid in full even though
# real text almost never contains a credential — measured 0 matches across 1,804
# live session-history messages (1.47 MB).
#
# So `_might_contain_credential` answers the cheap question "could a match exist
# at all?" and lets `redact_credentials` skip the expensive scan when the answer
# is no. It is a strict SUPERSET of `_CREDENTIAL_PATTERNS`, i.e. for every string
# the pattern matches, this returns True. That direction is the security
# property: a false POSITIVE only costs a scan we would have run anyway, while a
# false NEGATIVE would skip redaction and leak a credential into persisted chat
# history. Every condition below is therefore a NECESSARY condition of a branch,
# never a restatement of it — each is deliberately looser than the branch it
# stands in for.
#
# THE MAINTENANCE HAZARD this is built against: adding a 24th branch to
# `_CREDENTIAL_PATTERNS` without adding a matching anchor here would silently
# disable redaction for it. Nothing about the pattern edit would look wrong, and
# the failure is invisible in output — the branch simply stops firing. So
# `test_credential_prefilter.py` splits `_CREDENTIAL_PATTERNS.pattern` on its
# top-level `|`, asserts the branch count equals the number of registered sample
# credentials, and asserts the pre-filter fires for each. A new branch fails that
# count assertion loudly instead of quietly widening the leak.
#
# Literals are case-sensitive because the branches they stand for are (these
# prefixes are issued in a fixed case); the sole case-insensitive branch
# (`Authorization: Bearer`) is handled separately below.
_CREDENTIAL_PREFILTER_LITERALS: tuple[str, ...] = (
    "AKIA",  # AWS access key ID
    "ASIA",  # AWS access key ID (STS)
    "AccessKey",  # SecretAccessKey + AccessKeyId (shared substring)
    "aws_secret_access_key",
    "aws_session_token",
    "aws_access_key_id",
    "SessionToken",
    "PRIVATE KEY-----",  # PEM header AND footer both carry it
    "xox",  # Slack token
    "github_pat_",
    "glpat-",
    "k_live_",  # sk_live_ / rk_live_ (shared substring)
    "k_test_",  # sk_test_ / rk_test_ (shared substring)
    "SG.",  # SendGrid
    "sk-proj-",  # OpenAI
    "sk-ant-",  # Anthropic
    "npm_",
    "pypi-",
    "_v1_",  # do[opr]_v1_ DigitalOcean
    "GOCSPX-",  # Google OAuth client secret
    "eyJ",  # JWS / JWE / 2-segment link token
)

# Branches with no usable literal anchor. Each is the branch's own leading shape
# with its expensive tail dropped, so it stays a superset while keeping a narrow
# first-character set that `re` can skip on.
#   `gh[opsur]_`     — GitHub PAT family; a bare "gh" literal matches ordinary
#                      prose ("through", "might"), so the class is kept.
#   `[0-9]{6,}:…{30}` — Telegram bot token. The trailing 30-char run matters: a
#                      bare `[0-9]{6,}:` matches an epoch timestamp followed by a
#                      colon, which fired on 29 of 614 real messages.
#   `[MNO]…\.`        — Discord bot token (first segment is base64 of a snowflake).
#   `://…:…@`         — URI userinfo. The scheme alternation is dropped, which is
#                      what leaves a `://` literal prefix for `re` to search on;
#                      a bare `://` would match every ordinary URL.
_CREDENTIAL_PREFILTER_GH_RE = re.compile(r"gh[opsur]_")
_CREDENTIAL_PREFILTER_TELEGRAM_RE = re.compile(r"[0-9]{6,}:[A-Za-z0-9_-]{30}")
_CREDENTIAL_PREFILTER_DISCORD_RE = re.compile(r"[MNO][A-Za-z0-9_-]{22,30}\.")
_CREDENTIAL_PREFILTER_URI_RE = re.compile(r"://[^\s:/@]*:[^\s/]+@")

# The `Authorization: Bearer` branch is the ONLY case-insensitive branch, and it is
# spelled `(?i:Authorization)`. This anchor reuses that exact sub-pattern, so it is
# a superset of the branch BY CONSTRUCTION — same engine, same folding rules.
#
# `"authorization" in text.lower()` is NOT a valid anchor for it, because
# `str.lower()` and `re.IGNORECASE` are two DIFFERENT case-folding
# implementations and they disagree. `re` folds via `sre_compile._equivalences`,
# which treats U+0131 (LATIN SMALL LETTER DOTLESS I) and U+0130 (LATIN CAPITAL
# LETTER I WITH DOT ABOVE) as equivalent to `i`/`I`; `str.lower()` leaves U+0131
# unchanged and expands U+0130 to two code points. So the branch MATCHES
# `Authorızation: Bearer <token>` while a `.lower()` anchor MISSES it, which skips
# pass 1 and leaves the bearer token verbatim in persisted chat history. The same
# disagreement holds for U+017F/`s` and U+212A/`k`, so it is a class of defect
# rather than one homoglyph: a case-insensitive branch is only safely anchored by
# the SAME regex engine, never by a hand-rolled fold.
# Pinned by `test_unicode_case_folding_cannot_bypass_the_prefilter`.
_CREDENTIAL_PREFILTER_AUTHORIZATION_RE = re.compile(r"(?i:Authorization)")


def _might_contain_credential(text: str) -> bool:
    """Return True if *text* could contain a `_CREDENTIAL_PATTERNS` match.

    A strict superset of `_CREDENTIAL_PATTERNS.search(text) is not None`: it may
    return True where the pattern would not match, but it MUST NOT return False
    where the pattern would match. Callers use it only to skip a scan whose
    result is already known to be empty, so output is unchanged either way.
    """
    for literal in _CREDENTIAL_PREFILTER_LITERALS:
        if literal in text:
            return True
    return (
        _CREDENTIAL_PREFILTER_GH_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_TELEGRAM_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_DISCORD_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_URI_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_AUTHORIZATION_RE.search(text) is not None
    )


# Minimum string length at which `_might_contain_credential` is cheaper than the
# `_CREDENTIAL_PATTERNS` alternation it gates. The pre-filter has a fixed ~590 ns
# floor (21 substring searches plus 5 anchored regex calls) that does not shrink
# with the input, so on a very short string the alternation simply wins: measured
# 684 ns against 494 ns at 8 characters, crossing over at 12 and reaching 3.4x by
# 256. Callers scanning SHORT strings -- a decoded base64 blob is typically 16-30
# characters -- must gate on this rather than assume the pre-filter is
# unconditionally cheaper.
#
# Held at 16 rather than the measured crossover of 12, deliberately: the gate is
# verdict-neutral (the pre-filter is a proven superset, so either route reaches the
# same answer), which makes a conservative threshold cost at most one alternation
# scan on a 12-15 character blob and makes it robust to the crossover drifting as
# the pre-filter's own cost changes. It has already drifted once -- adding the
# case-insensitive Authorization anchor moved it from 16 to 12.
_PREFILTER_MIN_LEN = 16


# Base64 alphabet: at least 40 chars of [A-Za-z0-9+/] ending with optional =
_B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


# ── Label-independent bare-secret detection ──
# A 40-char AWS *secret access key* (the value paired with an AKIA/ASIA access
# key ID) is a bare run of the base64 alphabet with NO distinctive prefix and NO
# key= label, so none of the labelled/prefixed patterns in _CREDENTIAL_PATTERNS
# catch it when it appears standalone (e.g. echoed alone, in a log line, or in a
# JSON array element). We add a conservative, entropy-gated detector for this
# shape. This is the HIGHEST false-positive-risk redaction rule in the module, so
# it is deliberately over-gated: a token must clear EVERY gate below to be
# redacted. The gates are ordered cheapest-first.
#
# AWS secret access keys are exactly 40 base64 characters. We match ANY isolated
# run of >=40 base64-alphabet chars (word-boundary look-arounds keep surrounding
# prose intact and stop a longer high-entropy blob from being split and missed),
# then require the *specific 40-char secret shape* per token.
#
# NOT CONSULTED BY `redact_credentials`. Pass 3 derives its runs from
# `_B64_CHUNK_RE` instead (`run = chunk.rstrip("=")`), because that one scan feeds
# both pass 2 and pass 3 and the two patterns select identical spans. The only
# remaining consumer here is `_text_contains_bare_secret`. That split is a
# desync hazard: WIDENING THIS PATTERN ALONE (adding base64url `-_`, say) would
# change the URL scan and leave the redactor untouched, silently. Any edit to the
# character class or the `{40,}` floor must be mirrored in `_B64_CHUNK_RE` above.
# `test_the_two_base64_run_patterns_stay_structurally_coupled` pins both literals
# so such an edit fails loudly rather than drifting.
_BARE_SECRET_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}(?![A-Za-z0-9+/])")

# Exactly-40 is the AWS secret-key length. Keeping the shape check length-exact
# (rather than ">=40") is what lets the structural gates below cleanly separate
# real keys from 64-char sha256 hex, base64 document blobs, etc.
_SECRET_KEY_LEN = 40

# Shannon-entropy floor (bits/char). A uniformly-random 40-char base64 string
# averages ~4.78 bits/char and empirically almost never drops below ~4.4;
# English-word identifiers, hex digests, and repeated/low-alphabet runs sit
# below this. 4.3 is a conservative floor that admits real keys (the canonical
# AWS example scores 4.66) while rejecting camelCase code identifiers and file
# paths, which cluster around 4.0-4.3.
_SECRET_ENTROPY_MIN = 4.3

# Even after the entropy floor, camelCase / PascalCase code identifiers and
# slash-delimited file paths (e.g. src/main/java/com/Example/FooBarBazClas1) can
# survive on entropy ALONE. Two structural signals separate a random secret from
# a word-based identifier or path: (a) a random key almost never contains a long
# unbroken lowercase run, whereas identifiers/paths are built from dictionary
# words that do; (b) a random key has a low vowel ratio, whereas English words
# do not. NOTE: unlike a naive design we deliberately do NOT treat the presence
# of '/' or '+' as a free pass to redact — 40-char mixed-case file paths contain
# '/' yet are benign, so a '/' token must still clear both structural gates.
# Neither gate can speak for a path LONGER than one window, though: its straddling
# sub-windows are built from fragments of several components and clear both, which
# is what `_SECRET_MAX_SLASHES` below is for.
# Thresholds are chosen from measured distributions (see test_security.py) with a
# wide margin toward NOT redacting.
_SECRET_MAX_LOWER_RUN = 5
_SECRET_MAX_VOWEL_RATIO = 0.30

# Ceiling on the separators a window CUT OUT OF A LONGER RUN may hold, applied by
# :func:`_contains_bare_secret`. A window straddling several path components is a
# token nobody wrote, and it clears both gates above; its separator density is
# what gives it away -- `/` is 1 base64 character in 64, so a real 40-char key
# averages 0.6 of them, while a window spanning components carries one per
# component. Measured on 200,000 uniformly random 40-char keys: this declines
# 0.36% on its own, against 9.29% for the lowercase-run gate and 6.60% for the
# vowel-ratio gate. Three is the knee: four leaves the reported paths redacted.
_SECRET_MAX_SLASHES = 3

# The second ceiling a window CUT OUT OF A LONGER RUN is held to: it may not cross
# the opening `/` of a routed commit and take twelve of its digits. A commit
# permalink is one run (`com/Owner/RepoName/blob/<commit>/src/file`), and its window
# straddling `RepoName/blob/` and the start of the commit clears every gate on two
# separators: capitals from the repository name, lowercase and digits from the hex.
# A routed commit is a path segment of exactly 40 or 64 lowercase hex digits, as git
# prints one, right after one of the code-host routes below and ending at a `/` or
# the run's edge. The route is what keeps a key out: a key's own window crosses the
# opening `/` of a routed commit only when the key itself holds that `/` and twelve
# hex digits after it, which makes the text a permalink whose route runs into the
# key. A window starting inside a commit is judged as before, and so is hex a key
# carries anywhere else. Measured on ten commits of each of the 1,000 most-starred
# GitHub repositories: 3.58% of their blob permalinks fire without this and 0.02%
# with it, and none of 20,000 key-shaped random keys is lost glued before, after or
# inside a commit, after a letter, after a route, or in a URL path. The residuals
# are a repository name whose own capitals fill most of a window, a window that
# starts inside the commit and runs into a path with capitals (1.08% of links to a
# path like `src/File.py`, against 4.64% before), a key that carries a route word
# and a `/` followed by twelve or more hex digits running to its end,
# raw.githubusercontent.com links, which have no route word, and commits typed in
# upper or mixed case, which git never prints.
_ROUTED_COMMIT_RE = re.compile(
    r"(?:(?<=/)|\A)(?:blob|tree|blame|raw|commits?|src)/"
    r"(?P<commit>[0-9a-f]{64}|[0-9a-f]{40})(?=/|\Z)"
)
_ROUTED_COMMIT_PIECE_MIN = 12

# A token that base64-decodes to >=85% printable ASCII is encoded *text*, not a
# random key (random 40-char keys decode to mostly non-printable bytes). Such a
# token is left to the existing base64 decode-and-scan path in redact_credentials
# so we do not double-count or mis-classify it here.
_SECRET_PRINTABLE_DECODE_RATIO = 0.85

_VOWELS: frozenset[str] = frozenset("aeiouAEIOU")

# All-hex runs are git SHAs (40 hex), sha256 (64 hex), md5 (32 hex), etc. — never
# an AWS secret key (which uses the full base64 alphabet). Reject them outright.
_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")

# The macOS per-user directory id: ``confstr`` names the temp and cache roots
# ``/var/folders/<2>/<30>/T`` and ``.../C``, the two variable components an
# OS-generated lowercase encoding of the user's UUID and uid. ``/`` is in the run
# alphabet, so a path under them is one run whose window straddling the id and
# the ``T`` clears every gate, and every temp path -- a computer-use screenshot
# echoed as ``![](path)`` among them -- would read as a key. Pass 3 therefore
# scans the original run, exempting only windows that share at least
# ``_HOST_ID_EXEMPT_OVERLAP`` bytes IN PLACE with THIS host's OS-reported id. Any
# other positive window overlapping the id is still a hit, and pass 3 redacts
# every uncovered piece such a window touches; the id and the prefix stay
# plaintext because they are withheld from the scan, not redacted. Fast paths,
# the fragment separator ceiling and printable-base64 exclusion use whole-run
# context. In free text a writer picks an id's bytes and could fill most of a
# key-shaped window, while the host's id is chosen by nobody who writes text. The
# prefix must start a path (no run-alphabet byte, ``/`` included, before it) and
# the byte after the directory letter must not continue the run: ``/`` starts the
# next component, and any byte outside the run alphabet ends the run there, so a
# bare root printed, quoted, or followed by a newline is the same two-byte ``/T``
# piece as one that ends the text. ``/Tevil`` is a chosen name and gets no
# exemption. A key after ``/T_evil`` sits in the next run and is judged there;
# the pieces the exemption leaves in this run are the prefix and the ``/T``, both
# shorter than one key, so no key-shaped window survives in them.
#: Least overlap at which a key-shaped window is exempted from the bare-secret
#: scan: a key can occupy an exempt window only by sharing this many of its bytes
#: IN PLACE with an id the OS chose, which no writer controls. 24 exempts the
#: screenshot spooler path (32), the bare root (33) and an ``mkdtemp`` ``tmpXXXXXXXX``
#: dir with a one-character leaf (24); a temp path whose run goes 15 or more run-alphabet
#: bytes past the directory letter can still yield a positive window below 24 and stays
#: redacted, as without the exemption. A key glued across the id with less overlap is judged.
_HOST_ID_EXEMPT_OVERLAP = 24
_DARWIN_USER_DIR_PREFIX_RE = r"(?<![A-Za-z0-9+/])(?:/private)?/var/folders/"
_DARWIN_USER_DIR_SUFFIX_RE = r"/[CT](?![A-Za-z0-9+])"
# ``_CS_DARWIN_USER_TEMP_DIR`` in Darwin's ``<unistd.h>``; Python has no name for
# it, and its answer is the temp root with a trailing separator.
_CS_DARWIN_USER_TEMP_DIR = 65537
_DARWIN_USER_TEMP_DIR_RE = re.compile(
    r"\A(?:/private)?/var/folders/(?P<id>[a-z0-9_]{2}/[a-z0-9_]{30})/T/?\Z"
)

# The Shannon term ``(c / _SECRET_KEY_LEN) * log2(c / _SECRET_KEY_LEN)``, indexed by
# the character count ``c``. Element 0 is a ``0.0`` placeholder that keeps ``c``
# usable as a direct index; it is never read, because a count of zero cannot appear
# in a :class:`~collections.Counter` built from an iterable, and ``log2(0)`` would
# raise.
#
# Built for ONE length rather than parameterised over lengths, because
# :func:`_looks_like_secret_key` reaches the entropy gate only through its
# exactly-``_SECRET_KEY_LEN`` check, so that is the only length any production call
# can ask about. A per-length table would need a size cap and an eviction policy to
# bound what an arbitrary caller could materialise -- machinery guarding a caller
# that does not exist. Any other length falls through to the inline formula, which
# is what this table was derived from, so the general path is exactly as it was
# before the table existed.
#
# The terms are computed with the same operations the inline expression used, which
# is what makes this a pure precomputation rather than a re-derivation.
_ENTROPY_TERMS_KEY_LEN: tuple[float, ...] = (0.0,) + tuple(
    (c / _SECRET_KEY_LEN) * math.log2(c / _SECRET_KEY_LEN) for c in range(1, _SECRET_KEY_LEN + 1)
)


def _shannon_entropy(token: str) -> float:
    """Return the Shannon entropy of *token* in bits per character.

    The result is compared against :data:`_SECRET_ENTROPY_MIN` by
    :func:`_looks_like_secret_key`, so this is a gate on a redaction verdict and
    NOT a statistic anybody displays. A one-ULP drift at the boundary flips that
    verdict, and a flip in the permissive direction leaks a credential. The
    optimisation below is therefore built to be BIT-IDENTICAL, not merely close,
    and is pinned that way by ``TestShannonEntropyIsBitIdentical``.

    Each addend is ``(c / length) * log2(c / length)``. The sole production caller
    reaches this only through the exactly-``_SECRET_KEY_LEN`` check in
    :func:`_looks_like_secret_key`, and reaches it over and over --
    :func:`_contains_bare_secret` slides a 40-char window byte by byte across each
    base64-alphabet run that clears its prefilters -- so at that one length every
    addend is drawn from the fixed set :data:`_ENTROPY_TERMS_KEY_LEN` holds. That
    retires TWO true divisions and one ``math.log2`` call per DISTINCT CHARACTER per
    call -- ``c / length`` appears twice in the expression and CPython evaluates it
    twice, and a 40-char base64 window holds ~30 distinct characters -- plus the
    generator frames, in favour of a C-level ``map`` over a tuple index.

    Any other length takes the inline formula, unchanged from before the table
    existed. That keeps the fast path to the single length that is actually asked
    for, so no size cap or cache-eviction policy is needed to bound what an
    arbitrary caller could make this allocate.

    Why this is bit-identical rather than approximately equal:

    * Each addend is produced by the same three IEEE-754 operations on the same
      operands as before -- divide, ``log2``, multiply -- so each addend carries
      the same bit pattern. Precomputation changes WHEN a term is computed, never
      HOW.
    * ``Counter(token).values()`` still supplies the addends, in the same
      first-occurrence order, and ``map`` is consumed in order, so ``sum``
      accumulates identical addends in an identical sequence. The equality
      therefore does not rest on float addition being associative, which it is
      not. An algebraic rearrangement such as
      ``log2(length) - sum(c * log2(c)) / length`` IS mathematically equal and is
      measurably NOT bit-equal, which is why it is not used here.
    """
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    if length != _SECRET_KEY_LEN:
        return -sum((c / length) * math.log2(c / length) for c in counts.values())
    return -sum(map(_ENTROPY_TERMS_KEY_LEN.__getitem__, counts.values()))


def _has_all_three_char_classes(text: str) -> bool:
    """Return True if *text* holds at least one lowercase, uppercase AND digit.

    One pass with early exit, rather than three ``any()`` scans. Semantically
    identical, but this is the hottest predicate in the redaction path:
    :func:`_contains_bare_secret` slides a 40-char window BYTE BY BYTE across a
    base64-alphabet run that clears its prefilters, so a 512-char run reaching that
    loop asks this question 473 times. Three ``any()`` scans build three generators
    per call and cost the SUM of their three first-match offsets; one loop breaks on
    completion and costs the MAX. Both forms short-circuit, so the saving is
    generator frames plus that sum-vs-max difference.

    Absence of a class is closed under substring, which is what lets
    :func:`_contains_bare_secret` ask this about a whole run and retire every
    window at once.
    """
    has_lower = has_upper = has_digit = False
    for ch in text:
        if not has_lower and ch.islower():
            has_lower = True
        elif not has_upper and ch.isupper():
            has_upper = True
        elif not has_digit and ch.isdigit():
            has_digit = True
        if has_lower and has_upper and has_digit:
            return True
    return False


# The byte set counted as "printable" by :func:`_decodes_to_printable_text`: tab,
# LF, CR and the printable ASCII range 0x20-0x7E. Held as ``bytes`` so the count
# can be delegated to ``bytes.translate``, which runs in C.
_PRINTABLE_BYTES: bytes = bytes(sorted({0x09, 0x0A, 0x0D} | set(range(0x20, 0x7F))))


def _decodes_to_printable_text(token: str) -> bool:
    """Return True if *token* base64-decodes to mostly-printable ASCII.

    Encoded human-readable text (a base64 document blob) decodes to printable
    bytes; a random 40-char secret key decodes to mostly non-printable bytes. We
    use this to exclude encoded-text blobs from the bare-secret heuristic (they
    are handled by the existing decode-and-scan pass instead).
    """
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except Exception:
        return False
    if not raw:
        return False
    # Count the printable bytes by DELETING them in C and measuring what is left,
    # rather than testing every byte in a Python loop. ``translate(None, set)``
    # returns exactly the bytes NOT in *set*, so ``len(raw) - len(...)`` is the
    # member count -- an integer identity, so the ratio and the comparison below
    # are bit-identical to the previous per-byte sum (asserted against a verbatim
    # copy of that sum in ``test_printable_count_matches_the_per_byte_sum``,
    # including all 256 single-byte inputs exhaustively).
    #
    # This is the single most expensive operation in pass 3, because the helper
    # runs once per base64-alphabet run AND again per 40-char window as gate 7 of
    # `_looks_like_secret_key`, and the old loop cost scaled with the DECODED byte
    # count rather than with the 40-char window. Measured 14.6x at 48 bytes rising
    # to 69x at 1500; a 2 KB encoded blob fell from 86.3 us to 1.2 us, which is
    # 98% of what `_contains_bare_secret` spent on such a run.
    printable = len(raw) - len(raw.translate(None, _PRINTABLE_BYTES))
    return printable / len(raw) >= _SECRET_PRINTABLE_DECODE_RATIO


def _lowercase_run_exceeds(token: str, cap: int) -> bool:
    """Return True if any run of consecutive lowercase letters is longer than *cap*.

    Dictionary-word identifiers and file-path segments contain long lowercase
    word runs; a uniformly random base64 secret almost never does. This is the
    primary discriminator that keeps camelCase identifiers and mixed-case file
    paths out of the bare-secret heuristic.

    The only question the caller asks is whether the longest run EXCEEDS a
    threshold, so this stops at cap+1 rather than scanning the whole token to
    find the true maximum. On the tokens this gate exists to reject -- the ones
    with a long lowercase run -- it exits after a handful of characters instead
    of all 40, which measured 3.97 -> 1.65 us per window.
    """
    current = 0
    for ch in token:
        if ch.islower():
            current += 1
            if current > cap:
                return True
        else:
            current = 0
    return False


def _vowel_ratio(token: str) -> float:
    """Return the fraction of alphabetic characters in *token* that are vowels.

    Deliberately left in this two-pass comprehension form. A single-pass rewrite
    measured 1.18x -- about 0.4 us on a 2.89 us gate -- which does not justify
    replacing the clearest possible expression of "fraction of letters that are
    vowels", and would owe its own independent-oracle test. Its neighbour
    :func:`_lowercase_run_exceeds` WAS rewritten because that one measured 2.4x.
    Do not optimise this unmeasured.
    """
    letters = [ch for ch in token if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch in _VOWELS) / len(letters)


def _looks_like_secret_key(token: str) -> bool:
    """Return True if *token* has the shape of a bare AWS secret access key.

    Conservative, multi-gate classifier for a label-less 40-char base64 secret.
    Every gate must pass; the design bias is toward NOT
    redacting (a false negative merely reverts to today's behavior, a false
    positive corrupts benign output).

    Gates are ordered by MEASURED cost per rejection, cheapest-per-reject first.
    Every gate is a pure predicate whose failure returns False, so the order is
    verdict-neutral and can be chosen purely for cost. Measured on a corpus of
    1705 windows that clear gates 1-3 (cost per window, share of windows that
    gate rejects on its own):

        lowercase run   1.65 us   66.5%  ->  2.5 us per rejection
        vowel ratio     2.89 us   62.3%  ->  4.6 us per rejection
        entropy         8.48 us   54.5%  -> 15.5 us per rejection
        decode          3.01 us    0.0%  ->  rejected nothing in that corpus

    These numbers are a SNAPSHOT from one corpus on one machine: treat them as a
    relative ranking, not a budget, and do not turn them into assertions (this
    repo's CI enables coverage on 3.12 only, so absolute durations are not
    comparable across shards). The ordering is the durable claim, and it is
    guarded by a test that counts which gates get evaluated -- see
    ``TestSecretGateOrderIsCostOrdered``.

    Putting the two cheap structural gates ahead of the entropy computation, and
    the decode check last, halves the cost of gates 4-7 and measured -47% on
    ``redact_credentials`` end to end. Do not reorder these back into
    "structural last" without re-measuring: the structural gates are both
    cheaper AND higher-yield than entropy, which is the opposite of the
    intuition that entropy is the primary discriminator.

    1. Length is EXACTLY 40 (AWS secret-key length).
    2. Contains all three of lower + upper + digit (rejects all-lower prose runs,
       all-upper CONSTANT_NAMES, base32, digit strings).
    3. Not an all-hex run (rejects git SHAs, sha256/md5 digests).
    4. No lowercase run longer than _SECRET_MAX_LOWER_RUN.
    5. Vowel ratio <= _SECRET_MAX_VOWEL_RATIO. Gates 4 and 5 are the
       structural-randomness pair: they separate a random key from word-based
       identifiers and slash-delimited file paths that survive the entropy
       floor. Both apply to EVERY token (a '/' or '+' does not exempt a token,
       so 40-char mixed-case file paths stay intact).
    6. Shannon entropy >= _SECRET_ENTROPY_MIN (rejects low-entropy repeats/prose
       and most code identifiers, which cluster below 4.3).
    7. Does not base64-decode to printable text (rejects encoded-text blobs).
       Last because it is the lowest-yield gate, not because it is optional --
       it is what keeps legitimate OAuth ``code_challenge`` values in sign-in
       URLs from being redacted (guarded by the OAuth-URL corpus).

    BOUNDARY ASSUMPTION: this classifier deliberately evaluates an EXACTLY-40-char
    window (gate 1). It does NOT itself scan longer runs — a real key glued to an
    adjacent base64 char with no delimiter (e.g. ``X`` + key, key + ``A``,
    ``SECRET=`` + key + ``ABC``, key + ``X`` + key) forms a 41+ char run that would
    fail the exact-40 gate and leak verbatim. Callers that receive raw ``{40,}``
    runs MUST use :func:`_contains_bare_secret`, which slides a 40-char window
    across the run so a glued secret is still caught. Keep the exact-40 shape here:
    it is what lets the structural gates cleanly separate real keys from 64-char
    sha256 hex, base64 document blobs, etc.
    """
    if len(token) != _SECRET_KEY_LEN:
        return False
    if not _has_all_three_char_classes(token):
        return False
    if _HEX_ONLY_RE.match(token):
        return False
    if _lowercase_run_exceeds(token, _SECRET_MAX_LOWER_RUN):
        return False
    if _vowel_ratio(token) > _SECRET_MAX_VOWEL_RATIO:
        return False
    if _shannon_entropy(token) < _SECRET_ENTROPY_MIN:
        return False
    return not _decodes_to_printable_text(token)


def _contains_bare_secret(run: str) -> bool:
    """Return True if any 40-char window of *run* looks like a bare secret key.

    :func:`_looks_like_secret_key` only accepts an EXACTLY-40-char token, but the
    ``_BARE_SECRET_RUN_RE`` boundary look-arounds capture the longest possible run
    of base64-alphabet chars. A genuine 40-char secret glued to an adjacent
    base64 char with no delimiter (``X`` + key, key + ``A``, ``SECRET=`` + key +
    ``ABC``, key + ``X`` + key) produces a 41+ char run that would fail the
    exact-40 gate and leak verbatim. We slide a 40-char window across the run and
    report a hit if ANY window clears every gate. This stays linear in the run
    length (the regex yields disjoint spans), so cost is bounded overall.

    ENCODED-TEXT-BLOB EXCLUSION: if the WHOLE run base64-decodes to printable
    text it is a cohesive encoded blob (e.g. an OAuth/PKCE ``code_challenge``,
    which is ``base64(sha256-hex)``), not a bare secret — those are handled by
    the decode-and-scan pass instead. We must skip it here because sliding a
    40-char window byte-by-byte across such a blob creates base64-*misaligned*
    sub-windows whose garbage decode looks high-entropy and would clear every
    per-window gate, wrongly redacting a legitimate sign-in URL (regression
    guarded by the OAuth-URL corpus). This is the same bias-toward-not-redacting
    that :func:`_looks_like_secret_key` already applies per-window (gate 7),
    lifted to run granularity so a misaligned window cannot defeat it. A genuine
    glued secret (``X`` + key, key + ``ABC``, key + ``X`` + key) does NOT decode
    cleanly as a whole run, so it still reaches the sliding window below.

    SEPARATOR AND COMMIT CEILINGS FOR A FRAGMENT. ``/`` is in the run alphabet, so
    a deep absolute path, or a permalink carrying a commit hash, is one run whose
    straddling sub-windows clear every per-window gate. A key-shaped window
    carrying a path's separator density (``_SECRET_MAX_SLASHES``) or crossing into a
    routed commit (``_ROUTED_COMMIT_RE``, ``_ROUTED_COMMIT_PIECE_MIN``) is declined,
    on two conditions that keep this a gate rather than a hole: only when the run is
    LONGER than one whole key (a 40-char run IS the token somebody wrote, so a
    standalone key is never subject to it), and only AFTER
    :func:`_looks_like_secret_key` has answered, so every offset is still classified
    and a glued key is still found at its own offset.
    """
    return next(_bare_secret_window_starts(run), None) is not None


def _bare_secret_window_starts(
    run: str, skip_spans: tuple[tuple[int, int], ...] = ()
) -> Iterator[int]:
    """Omit windows with >= _HOST_ID_EXEMPT_OVERLAP bytes in a run-relative *skip_spans* entry."""
    if len(run) < _SECRET_KEY_LEN:
        return
    # RUN-LEVEL FAST PATH. Two of the per-window gates reject on a property that
    # is closed under substring, so asking about the whole run once can retire
    # every window without classifying any of them:
    #   gate 2 -- a character class absent from the run is absent from all of its
    #             substrings, so no window can hold all three;
    #   gate 3 -- every substring of an all-hex run is itself all-hex.
    # Both answers are False either way, so this only reorders WHICH check
    # returns False, never the verdict. Guarded on a run longer than one window,
    # because at exactly 40 chars the sole window pays the same two gates anyway
    # and the pre-check would be pure duplicate work. This is what keeps the
    # slide affordable on long non-secret runs (hex digests, lowercase blobs),
    # which are the common shape in tool output.
    is_fragment = len(run) > _SECRET_KEY_LEN
    if is_fragment:
        if not _has_all_three_char_classes(run):
            return
        if _HEX_ONLY_RE.match(run):
            return
    if _decodes_to_printable_text(run):
        return
    commit_starts = (
        [match.start("commit") for match in _ROUTED_COMMIT_RE.finditer(run)] if is_fragment else []
    )
    for start in range(len(run) - _SECRET_KEY_LEN + 1):
        if skip_spans and any(
            min(start + _SECRET_KEY_LEN, end) - max(start, begin) >= _HOST_ID_EXEMPT_OVERLAP
            for begin, end in skip_spans
        ):
            continue
        window = run[start : start + _SECRET_KEY_LEN]
        if not _looks_like_secret_key(window):
            continue
        if is_fragment and (
            window.count("/") > _SECRET_MAX_SLASHES
            or _crosses_into_a_routed_commit(start, commit_starts)
        ):
            # Key-shaped, but a fragment carrying a path's separators or a commit.
            continue
        yield start


def _crosses_into_a_routed_commit(start: int, commit_starts: list[int]) -> bool:
    """Whether the window at *start* crosses a routed commit's opening ``/`` and
    takes ``_ROUTED_COMMIT_PIECE_MIN`` of its digits.

    Commits are a whole window long and a route apart, so only the first one
    starting after *start* can be crossed: one bisect, however many the run holds.
    """
    index = bisect.bisect_right(commit_starts, start)
    return (
        index < len(commit_starts)
        and commit_starts[index] + _ROUTED_COMMIT_PIECE_MIN <= start + _SECRET_KEY_LEN
    )


def _decode_b64_chunk(chunk: str) -> str:
    """Decode ONE `_B64_CHUNK_RE` match; return decoded credential text or ''.

    Equivalent to `_decode_b64_safe(chunk)` when *chunk* is itself a
    `_B64_CHUNK_RE` match, but without re-scanning it. `_decode_b64_safe` exists
    to find chunks inside arbitrary text; re-running that scan over a string that
    IS already one chunk can only rediscover the same single span —
    `[A-Za-z0-9+/]{40,}` is greedy so it consumes the whole run, and `={0,2}`
    takes the padding — so the inner `finditer` was pure duplicate work on the
    hot path, once per base64-looking run in every redacted message.
    """
    # NO LENGTH SHORT-CIRCUIT HERE, deliberately. It is tempting to skip the decode
    # when `len(chunk) % 4` is non-zero, on the reasoning that `validate=True`
    # rejects a length that is not a multiple of 4. That reasoning is INTERPRETER
    # DEPENDENT and would be a redaction bypass: `binascii.a2b_base64`'s padding
    # leniency changed with `strict_mode`, so on Python 3.10 and 3.11 a chunk of 40
    # data characters plus one `=` (length 41) DECODES, while on 3.12 it raises.
    # Skipping it would leave a base64-encoded credential in that shape unredacted
    # on exactly the interpreters CI still builds. No version-invariant form of the
    # test exists either -- 43 data characters plus `==` decodes on 3.10 while
    # failing both a total-length and a stripped-length predicate. Pinned by
    # `test_a_decode_length_precondition_would_be_version_dependent`.
    try:
        decoded = base64.b64decode(chunk, validate=True).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Gate the alternation behind the cheap superset pre-filter, exactly as pass 1
    # does. `_might_contain_credential` may return True where the pattern would not
    # match but never False where it would, so the verdict cannot move -- only the
    # cost. Real decoded blobs almost never look like credentials: 0 of 18 in the
    # session corpus and 1 of 849 in a hash-heavy corpus reach the alternation.
    #
    # LENGTH-GATED, because here the pre-filter is NOT unconditionally cheaper. Its
    # ~540 ns floor is fixed while the alternation's cost scales with length, so
    # below `_PREFILTER_MIN_LEN` the alternation wins outright. A decoded blob is
    # exactly the size where that matters -- 48 raw bytes from a 64-char run, and
    # shorter once `errors="ignore"` drops invalid sequences, measured 12-31
    # characters -- so this straddles the crossover instead of sitting above it.
    if len(decoded) >= _PREFILTER_MIN_LEN and not _might_contain_credential(decoded):
        return ""
    return decoded if _contains_credential_pattern(decoded) else ""


def _decode_b64_safe(text: str) -> str:
    """Try to base64-decode chunks in text; return decoded content or ''.

    Deliberately left UNOPTIMISED. `_decode_b64_chunk` above is the hot-path
    single-chunk form, and this function is what pins it: the differential test
    asserts the two agree on every chunk in the corpus, and the pre-optimisation
    reference oracle calls this one. Applying the same gates here would make both
    sides of that comparison share the change and the check would stop detecting
    anything.
    """
    for m in _B64_CHUNK_RE.finditer(text):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
            if _contains_credential_pattern(decoded):
                return decoded
        except Exception:
            continue
    return ""


def _contains_fixed_credential(text: str) -> bool:
    """Return True for canonical literal or base64-encoded credentials.

    Deliberately excludes the bare 40-character entropy heuristic. OAuth
    front-channel state and PKCE values are high-entropy by design, while the
    canonical signatures and decoded credentials remain unambiguous.
    """
    return bool(_contains_credential_pattern(text) or _decode_b64_safe(text))


def _text_contains_bare_secret(text: str) -> bool:
    """Return True when *text* contains an isolated bare AWS-secret run."""
    return any(_contains_bare_secret(match.group()) for match in _BARE_SECRET_RUN_RE.finditer(text))


# ── Document links the bare-secret pass leaves alone ──
# `/` is in the run alphabet, so in `https://docs.google.com/document/d/<id>/edit`
# the whole of `com/document/d/<id>/edit` is ONE run, and a Google document id is
# ~44 uniformly random base64url characters: a 40-char window inside it clears
# every gate of `_looks_like_secret_key`. No entropy or structural gate can tell
# that id from a key, so the decision is made on the link instead. Pass 3 skips a
# run only when the run lies wholly inside a match of `_DOCUMENT_LINK_RE`, which
# admits nothing but a fixed route on a fixed host:
#   * HTTPS, a lowercase literal host right after the scheme (no userinfo, no
#     port), `docs.google.com`, `drive.google.com`, `<tenant>.atlassian.net`, or
#     a PlantUML server: a host whose first label is `plantuml` (after an
#     optional `www.`), such as `www.plantuml.com`;
#   * every path segment fixed or drawn from a closed class: a Google id is
#     25-72 chars of `[A-Za-z0-9_-]`, a Confluence space key is alphanumeric, a
#     page id is digits, a PlantUML diagram is `[/plantuml]/<png|svg|txt|uml>/`
#     then its deflate text in PlantUML's `[0-9A-Za-z_-]` alphabet;
#   * no piece of the link is a whole key: a piece exactly 40 characters long
#     voids the match when `_looks_like_secret_key` accepts it. Pieces are cut
#     twice, once at every non-run character (`/`, `-`, `_`, `%`, `.`, `~`)
#     and once also at `+`, the space of a Confluence title slug, so a key
#     pasted in as the id or as a title word is still redacted. A PlantUML
#     diagram is random-looking by construction, so it is judged by decoding
#     instead: it must inflate, whole, to printable text with nothing after the
#     stream but the encoder's zero padding, and every credential pass must
#     leave that text unchanged. The text inflated per call is capped (see
#     `_PLANTUML_SOURCE_CAP`). A key as the diagram, glued to one, or written
#     in its source voids the match;
#   * the link ends where the route ends, with one optional trailing `/`: a
#     further base64-alphabet character (a glued key, another `/segment`)
#     fails the match.
# The query and fragment are outside the match, so a key there, a `?token=`
# value and every pass-1 and pass-2 hit are judged exactly as before.
#
# ACCEPTED RESIDUAL: a markerless key without `/` glued to more letters or
# digits inside the id or the page title, or carrying its own `+` inside a
# title, is indistinguishable from the text it sits in and is not redacted.
# About 2% of real Google ids carry a key-shaped 40-char piece between their
# random `-`/`_` and stay redacted. That placement is not an accidental leak,
# and pass 3 is no control against a deliberate one: a single `-` already
# splits any run.
_DOCUMENT_ID = r"[A-Za-z0-9_-]{25,72}"
_PLANTUML_DIAGRAM = r"[A-Za-z0-9_-]{1,16384}"
_GOOGLE_USER = r"(?:u/[0-9]{1,2}/)?"
_RUN_PIECE_RES = (re.compile(r"[A-Za-z0-9+]+"), re.compile(r"[A-Za-z0-9]+"))
_DOCUMENT_LINK_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])https://(?:"
    r"docs\.google\.com/(?:document|spreadsheets|presentation|drawings|forms)/"
    + _GOOGLE_USER
    + r"d/(?:e/)?"
    + _DOCUMENT_ID
    + r"(?:/(?:edit|view|preview|copy|viewform|pub|pubhtml|htmlview))?"
    r"|drive\.google\.com/"
    + _GOOGLE_USER
    + r"(?:file/d/"
    + _DOCUMENT_ID
    + r"(?:/(?:view|edit|preview))?"
    r"|drive/" + _GOOGLE_USER + r"folders/" + _DOCUMENT_ID + r")"
    r"|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.atlassian\.net/wiki/spaces/~?[A-Za-z0-9]{1,64}/"
    r"(?:overview|pages/(?:edit-v2/)?[0-9]{1,20}"
    r"(?:/[A-Za-z0-9+%._~-]{1,255})?)"
    r"|(?:www\.)?plantuml(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?:/plantuml)?/(?:png|svg|txt|uml)/"
    r"(?P<diagram>" + _PLANTUML_DIAGRAM + r")"
    r")/?(?![A-Za-z0-9+/_-])"
)
_PLANTUML_ALPHABET = {
    c: i for i, c in enumerate("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_")
}
#: Decoded diagram text one call may inflate, in total, and per encoded
#: character. Real diagrams stay under 10x; anything larger keeps its link
#: masked, which is how it was judged before this route. The total bounds the
#: extra scan a call can be made to do to that of a plain text this long.
_PLANTUML_SOURCE_CAP = 16384
_PLANTUML_RATIO_CAP = 16
#: Set while a decoded diagram is being scanned, so a diagram link inside it is
#: judged as any other link and decoding never nests.
_IN_DIAGRAM_SCAN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_IN_DIAGRAM_SCAN", default=False
)


def _plantuml_source(encoded: str, limit: int) -> str | None:
    """Return the diagram text *encoded* inflates to, whole, or None."""
    if limit <= 0 or len(encoded) % 4:
        return None
    bits = [_PLANTUML_ALPHABET[c] for c in encoded]
    raw = bytearray()
    for i in range(0, len(bits), 4):
        word = bits[i] << 18 | bits[i + 1] << 12 | bits[i + 2] << 6 | bits[i + 3]
        raw += word.to_bytes(3, "big")
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        source = inflater.decompress(bytes(raw), limit).decode("utf-8")
    except (zlib.error, UnicodeDecodeError):
        return None
    # The encoder emits whole 4-character groups and pads only the last one,
    # with at most two zero bytes. A further group, a stream cut short, or one
    # over the limit is not a whole diagram, so text glued after a diagram, or a
    # diagram cut mid-group, is never kept.
    padding = inflater.unused_data
    if not inflater.eof or len(padding) > 2 or padding.strip(b"\0") or not source.strip():
        return None
    if not all(c.isprintable() or c in "\t\n\r" for c in source):
        return None
    return source


def _plantuml_verdict(encoded: str, limit: int) -> tuple[bool, int]:
    """Judge one diagram; also return how much text was inflated for it."""
    source = _plantuml_source(encoded, min(limit, _PLANTUML_RATIO_CAP * len(encoded)))
    if source is None:
        return False, min(limit, _PLANTUML_RATIO_CAP * len(encoded))
    # The link carries its source, so the source is judged by every credential
    # pass; anything they would mask leaves the link masked. The source is a
    # document of its own: the pass reads it from a fresh line start, never from
    # the line state the stream carries for the text AROUND the link (under a
    # carried `'` the quote opening a value read as that literal's close, the
    # assignment stood empty, and a diagram the batch pass masked streamed out).
    previous = _IN_DIAGRAM_SCAN.get()
    _IN_DIAGRAM_SCAN.set(True)
    line_state = _LINE_STATE.set(_LINE_START)
    try:
        return redact_credentials(source)[0] == source, len(source)
    finally:
        _LINE_STATE.reset(line_state)
        _IN_DIAGRAM_SCAN.set(previous)


def _document_link_spans(text: str) -> list[tuple[int, int]]:
    """Return the spans of the :data:`_DOCUMENT_LINK_RE` matches that hold no key."""
    spans = []
    budget = 0 if _IN_DIAGRAM_SCAN.get() else _PLANTUML_SOURCE_CAP
    for m in _DOCUMENT_LINK_RE.finditer(text):
        if m["diagram"]:
            kept, spent = _plantuml_verdict(m["diagram"], budget)
            budget -= spent
        else:
            kept = not any(
                len(piece) == _SECRET_KEY_LEN and _looks_like_secret_key(piece)
                for piece_re in _RUN_PIECE_RES
                for piece in piece_re.findall(m.group())
            )
        if kept:
            spans.append(m.span())
    return spans


#: Code-point ranges of the printable-ASCII tail of the standard baseline JPEG AC
#: Huffman symbol table -- the ``HUFFVAL`` list of ITU-T T.81 Annex K, Table K.5 --
#: in the order the table writes them. Everything before and after this tail is
#: outside printable ASCII, so a container holding the standard table delimits
#: exactly these characters with its own non-text bytes.
#:
#: Assembled from the ranges rather than pasted as a literal on purpose: the
#: assembled string IS the credential shape this masker exists to cancel, so a
#: pasted copy reads as key material to every secret scanner over this file.
_BASELINE_SYMBOL_TABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x25, 0x2A),
    (0x34, 0x3A),
    (0x43, 0x4A),
    (0x53, 0x5A),
    (0x63, 0x6A),
    (0x73, 0x7A),
)

#: The ONE fixed 45-character string the container false positive is made of.
#: Masking is pinned to this value, which is what bounds the masker: the only
#: characters it can ever remove are characters of this constant, so no
#: attacker-supplied byte is removable and no generated credential is maskable.
_BASELINE_SYMBOL_TABLE = "".join(
    chr(code) for low, high in _BASELINE_SYMBOL_TABLE_RANGES for code in range(low, high + 1)
)

#: Shortest slice of :data:`_BASELINE_SYMBOL_TABLE` any detector in this module
#: flags. Measured against the catalogue, not chosen: every shorter slice already
#: passes the scan unmasked, so masking one could not change an answer, and
#: ``test_no_shorter_slice_of_the_table_is_flagged`` fails if that stops holding.
#: It is the region floor below, which is what keeps this cheap on real media: a
#: 400 KB photograph holds tens of thousands of text runs and exactly one this
#: long.
_BASELINE_SYMBOL_TABLE_MIN = 37

#: Masked regions retained before this gives up and returns the buffer UNMASKED.
#: The bound matters because the region floor is 37 text bytes, so a crafted 50 MB
#: upload could carry over a million qualifying regions and the retained slices
#: would amplify it several times over in memory. Real containers are nowhere near
#: it -- one table per embedded image -- and exceeding it returns the unmasked
#: buffer, which is the REFUSING direction: the scan then answers exactly as it
#: did before this masker existed.
_MASKED_REGION_CAP = 4096

#: Substituted for the ALPHANUMERIC characters of a masked table; its punctuation
#: is copied through untouched. See :func:`_mask_region`, which explains why the
#: split is there.
#:
#: A tilde, and the property that matters is which character classes admit it, in
#: BOTH directions, because blanking a region can break a match as well as build
#: one.
#:
#: It must be admitted by every value class that can cross the region's boundary.
#: A credential's match can ANCHOR ACROSS a masked region: the non-text bytes that
#: delimit the region are themselves inside ``[^\s/]+``, ``[^\s"',}]+`` and
#: ``[^\s:/@]*``, so a URL password can begin before a table and reach its ``@``
#: after it. A filler those classes reject -- a space, most obviously -- TERMINATES
#: that run, and the match the raw bytes had disappears from the scanned copy.
#: ``~`` is admitted by all three, so a crossing match survives masking intact.
#:
#: It must be admitted by no class that builds a contiguous token: not
#: ``[A-Za-z0-9+/]`` (``_BARE_SECRET_RUN_RE``), ``[A-Za-z0-9_-]``, ``[0-9]`` or a
#: PEM body. ``~`` is in none of them, so blanking can only SHORTEN such a run,
#: never lengthen one into a match the raw bytes lacked -- which is also what
#: cancels the table's own bot-token shape, the point of masking at all.
_MASKED_TABLE_FILLER = "~"

#: A maximal region of TEXT bytes -- the only bytes a credential can be written
#: in. Maximality is load-bearing rather than an optimisation: a region is bounded
#: by non-text bytes, so a region equal to the table is one a container delimited,
#: and any credential written beside a table shares the table's region and makes
#: it unmaskable.
#:
#: The floor is :data:`_BASELINE_SYMBOL_TABLE_MIN`, so the regex engine skips a
#: shorter run in one C-speed pass and Python never sees it.
_TEXT_REGION_RE = re.compile(r"[\t\n\r\x20-\x7e]{%d,}" % _BASELINE_SYMBOL_TABLE_MIN)


def _mask_region(region: str) -> str:
    """*region* with its alphanumerics filled and its punctuation copied through.

    A credential's match does not have to lie inside the region, and it can depend
    on the region in two different ways. One is a value RUN that crosses the
    boundary, which :data:`_MASKED_TABLE_FILLER` is chosen to survive. The other is
    a required LITERAL the match borrows FROM the region, and no choice of filler
    can survive that -- removing the character removes the literal.

    The standard table's printable tail contains ``:``, so a URL can borrow it:
    ``://[^\\s:/@]*:[^\\s/]+@`` matches with ``[^\\s:/@]*`` running from ``://``
    into the region, the separator ``:`` being the TABLE's own, and ``[^\\s/]+``
    running out of the region to the password and its ``@``. Fill the region and no
    colon follows ``://`` at all, so a password the raw scan refused is delivered.

    Splitting on alphanumeric is what settles this as a rule rather than one more
    exception. What masking exists to cancel is the table's credential SHAPE, and
    every unlabelled shape in the catalogue is built from alphanumerics -- six
    digits then thirty-two letters, a forty-character base64 run. What a pattern
    can require as a literal is punctuation. So filling only the alphanumerics
    removes the whole shape while leaving every literal the region could ever lend,
    which for this constant is ``%&'()*:`` rather than the one colon that happened
    to be found.

    The shape really does die: the bot-token form needs ``[0-9]{6,}`` before its
    colon and the bare-secret form needs forty characters of ``[A-Za-z0-9+/]``, and
    :data:`_MASKED_TABLE_FILLER` is in neither class, so a region of filler and
    punctuation matches no detector in this module.
    """
    return "".join(_MASKED_TABLE_FILLER if char.isalnum() else char for char in region)


def mask_baseline_symbol_tables(text: str) -> str:
    """Blank the standard container symbol tables in *text*, leaving all else.

    For the BINARY delivery scans only. A JPEG's ``DHT`` segment carries the
    standard baseline Huffman symbol table, and that table's printable tail reads
    as six digits, a colon and thirty-two letters -- exactly the shape of an
    unlabelled bot token. Every image written with the default tables holds it,
    including a blank 694-byte one carrying no metadata at all, so the shared
    binary delivery scan refuses essentially every such image.

    The detectors cannot see this on their own. Their entropy floor scores the
    character multiset, and the table's characters are all distinct, so it scores
    the full bits per character a generated secret does.

    Safety rests on two bounds, and one alone is not enough. WHAT MAY BE REMOVED:
    a region is masked only when it EQUALS a contiguous slice of
    :data:`_BASELINE_SYMBOL_TABLE`, one fixed public 45-character constant, so the
    only characters this function can remove are characters of that constant and a
    value is maskable only if whoever wrote it already knows it. No reasoning about
    shapes is involved, which is the point: a credential-shaped run that merely
    resembles a table -- ascending, high-diversity, delimited -- is not a slice of
    the constant and keeps its whole match.

    WHAT THE REMOVAL MAY BREAK is the second bound, and it belongs to
    :func:`_mask_region` rather than to the region test. A credential's match can
    depend on the region without lying inside it: it can ANCHOR ACROSS it, because
    the non-text bytes delimiting the region are inside the value classes that
    carry no literal label, and it can BORROW a required literal from it, because
    the constant contains punctuation such as ``:``. So removing only PUBLIC
    characters can still destroy a match. Both are closed there: the filler is
    admitted by every boundary-crossing class and by no contiguous-token class, so
    a crossing run survives and a token run can only shorten, and only the region's
    alphanumerics are filled, so every literal it could lend stays in place.

    Whole-region equality is what keeps a table from covering for a neighbour.
    Regions are maximal, so a table written next to a credential shares one region
    with it, that region is longer than the constant, and neither is masked. The
    same holds in the other direction: a credential whose match runs INTO the
    table's characters cannot have those characters taken away, because the region
    carrying both is not a slice of the constant either.

    The TEXT path deliberately keeps the unmasked scan. There a match costs a
    redaction tag; here it costs the delivery of the file, and the only way past
    that refusal is the durable class-wide grant in
    :mod:`kiro_crew.file_delivery_consent`, which then disarms the refusal for
    every content kind on every owner-facing gate. Noise on this path spends the
    control, so this path is where the noise has to go.

    Returns *text* ITSELF when nothing is masked, which the callers read as
    identity to skip re-scanning a buffer whose answer they already hold.
    """
    pieces: list[str] = []
    cursor = 0
    masked = 0
    for match in _TEXT_REGION_RE.finditer(text):
        if match.group() not in _BASELINE_SYMBOL_TABLE:
            continue
        masked += 1
        if masked > _MASKED_REGION_CAP:
            # Bounded, and bounded towards refusal: the caller re-scans the
            # unmasked buffer and answers as it did before this masker existed.
            return text
        start, end = match.span()
        pieces.append(text[cursor:start])
        pieces.append(_mask_region(text[start:end]))
        cursor = end
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


# ── `?token=` / `&token=` URL parameter values (pass 4) ──
# Keyed on the parameter NAME, not the value's shape, so an OPAQUE bearer value
# -- one that looks nothing like a JWT -- is redacted where every shape-based
# pattern above sees ordinary text. A parameter name is a context: `?token=`
# cannot match a filename, an identifier or a sourcemap name, so this adds
# coverage without inheriting shape-based false positives (the five measured
# `eyJ…` lookalikes recorded on the two-segment link-token alternative).
#
# Group 1 is the VALUE, and only the value is replaced: `token=` stays visible
# so a redacted URL still reads as a token URL. The precedent is
# `instances/token_mint._TOKEN_RE` (`[?&]token=([^\s&]+)`), which one caller
# kept privately because this module lacked the pass; the value class here is
# WIDER-terminated per the issue's requirement -- it also stops at quotes and
# `#` so a match cannot run past the parameter into a quoted string or a URL
# fragment -- and additionally excludes the RFC 3986-forbidden bytes
# (`<>{}|\^` and backtick): no legal URL query can carry them, while SOURCE
# and DOC text quoting a token URL does (`?token={token}` in an f-string,
# `` ?token=` `` in markdown, `?token=<your-token>` in prose). Without the
# exclusion, pass 4 matches the template placeholder and the chip-diff path
# (`chat_runner.py`) redacts a snapshot of `dashboard/urls.py` IN PLACE with
# no recovery -- the exact non-cosmetic false-positive surface this module
# cites as its reason for refusing to relax the JWT floor. A template whose
# value starts with an excluded byte now yields an empty value and no match.
#
# The parameter NAME folds ASCII case (`(?ai:token)`) -- unlike `eyJ`, a
# parameter name is not a fixed encoding prefix, and `?Token=` / `?TOKEN=`
# from a third-party provider carries the same bearer value. ASCII scope keeps
# Unicode lookalikes such as the Kelvin sign from spoofing the parser-visible
# name; the value bytes are still matched exactly as written.
#
# ACCEPTED RESIDUAL: a template value made of LEGAL query bytes
# (`?token=$TOKEN`, `?token=%s`) still matches and is redacted -- the class
# excludes only bytes no legal query can carry, and a shape test on the value
# would reintroduce the false-negative lever this pass exists to avoid.
#
# Deliberately NOT a `_CREDENTIAL_PATTERNS` branch: `_contains_fixed_credential`
# -- which gates request-BLOCKING decisions in `exfil.py` -- searches
# `_CREDENTIAL_PATTERNS`, so a branch would turn every `?token=` URL into a
# blocked request: a behaviour change the issue explicitly excludes. This pass
# redacts output only; the blocking surface is unchanged. The other
# credential-bearing parameter names (`access_token`, `id_token`, `api_key`,
# `code`) are excluded on the issue's own scoping ground -- each name wants its
# own false-positive analysis (`code=` especially collides with OAuth
# authorization codes AND ordinary prose) -- not because adding them HERE would
# change the blocking surface; a pass-4 name never feeds
# `_contains_fixed_credential`.
_TOKEN_PARAM_VALUE_CLASS = r"[^\s&\"'#<>{}|\\^`]"

_HTML_REF_AMP = (
    r"&(?:amp;|AMP;|amp(?![0-9A-Za-z])|AMP(?![0-9A-Za-z])"
    r"|#0{0,8}38(?:;|(?![0-9;]))|#[Xx]0{0,8}26(?:;|(?![0-9A-Fa-f;])))"
)
_HTML_REF_QUEST = r"&(?:quest;|#0{0,8}63(?:;|(?![0-9;]))|#[Xx]0{0,8}3[Ff](?:;|(?![0-9A-Fa-f;])))"
_HTML_REF_EQUALS = r"&(?:equals;|#0{0,8}61(?:;|(?![0-9;]))|#[Xx]0{0,8}3[Dd](?:;|(?![0-9A-Fa-f;])))"
_TOKEN_PARAM_SEP_ENTITY_RE = rf"(?:{_HTML_REF_AMP}|{_HTML_REF_QUEST})"
_TOKEN_PARAM_SEP_RE = rf"(?:[?&]|{_TOKEN_PARAM_SEP_ENTITY_RE})"
_TOKEN_PARAM_EQ_RE = rf"(?:=|{_HTML_REF_EQUALS})"


# Apply one standard decode per pipeline stage. An HTML parser decodes these
# references into structure BEFORE handing an attribute value to a query parser,
# while that query parser splits on raw separators BEFORE percent-decoding the
# parameter name. HTML references are therefore structure here, while encoded
# `%26` / `%3D` remain later-stage data rather than separators.
#
# Deliberate declines follow the HTML5 parser's actual table and attribute state:
# `&amptoken=` is unchanged because semicolon-less `&amp`/`&AMP` is decoded
# only before a NON-alphanumeric per the WHATWG flush rule; `&Amp;`,
# `&quest`, and `&equals` are absent from the table; `&amp;amp;token=` decodes only once to a non-token parameter name;
# and `%26token=` / `?token%3D` are data when the query parser performs its split.
#
# Numeric references stop at eight leading zeros. An unbounded `0*` would make
# the streaming WEAK holdback unbounded, so this is a DoS bound rather than a
# claim that longer spellings differ in the HTML specification.
# Each letter composes bounded HTML references over its literal byte and all
# three bytes of its percent escape.
def _html_numeric_refs(cp: int) -> list[str]:
    """The two bounded HTML numeric spellings of one code point.

    Mirrors `_HTML_REF_AMP`'s shape byte for byte, including the <=8-leading-zero
    DoS bound and the WHATWG flush rule (a semicolon-less numeric reference is
    decoded when the next byte cannot extend the number).

    The `;` is excluded from the zero-width alternative so a PRESENT semicolon
    MUST be consumed by the reference. Without it the engine can backtrack the
    reference to its semicolon-less branch and hand the `;` to a FOLLOWING pattern
    that accepts it: on `?token&#61;` with an empty value, `_TOKEN_PARAM_RE`'s EQ
    gave up its `;` and the value class captured it, so pass 4 spliced the
    credential tag over the semicolon in text `chat_runner.py` redacts IN PLACE.
    A real parser never leaves the terminator behind (`&#61;` decodes to `=`,
    `&#61;;` to `=;`), so the zero-width branch with `;` next models a decode no
    parser performs. Today only `_HTML_REF_EQUALS` is reachable -- `;` matches no
    name-letter, nibble, or separator alternative -- but the exclusion is uniform
    in this generator so the invariant is structural rather than per-site. The
    named `amp`/`AMP` lookaheads are deliberately NOT changed: `amp;` is ordered
    first and wins on every match, and the name position rejects `;`.
    """
    return [
        rf"&#0{{0,8}}{cp}(?:;|(?![0-9;]))",
        rf"&#[Xx]0{{0,8}}{cp:x}(?:;|(?![0-9A-Fa-f;]))",
    ]


def _html_or_literal(chars: str) -> str:
    """One anchor byte: its literal spellings, or an HTML reference to any of them.

    The literal class is left for the surrounding scoped `(?ai:...)` to fold, as
    the percent ladder already relies on. A numeric reference carries DIGITS,
    which no case fold reaches, so a letter byte emits references for BOTH cases
    explicitly -- without that, `%6&#102;` matched while `%6&#70;` did not.
    """
    literals = sorted(set(chars))
    alternatives = ["[" + "".join(literals) + "]" if len(literals) > 1 else literals[0]]
    for char in literals:
        alternatives += _html_numeric_refs(ord(char))
        if char.isalpha():
            alternatives += _html_numeric_refs(ord(char.swapcase()))
    return "(?:" + "|".join(alternatives) + ")"


#: `%` at the HTML stage. `&percnt;` REQUIRES its semicolon: unlike `amp`, it is
#: absent from the 106-entry semicolon-less legacy set, so `&percnt74` is data.
#: Named references are case-sensitive, so disable the surrounding name ladder's
#: ASCII case fold for this literal while numeric references keep folding `X`.
_PERCENT_SIGN_RE = "(?:" + "|".join(["%", *_html_numeric_refs(0x25), "(?-i:&percnt;)"]) + ")"


def _token_name_letter(letter: str) -> tuple[str, str]:
    """(complete, partial) spellings of one `token` letter.

    COMPLETE is every spelling that decodes to the letter: the literal
    (ASCII-case folded by the caller's `(?ai:...)`), an HTML reference to either
    case, and a percent escape whose three bytes are EACH spellable at the HTML
    stage -- the composition the two modelled stages admit (`&#37;74`,
    `%&#55;&#52;`). ASCII case differs in the HIGH nibble only, so the low nibble
    is case-invariant and the high nibble is a two-digit class.

    PARTIAL adds every end-of-chunk prefix whose last byte is NOT in
    `_CRED_CLASS` -- i.e. one ending at a `;` -- because `natural_cut` already
    holds every other prefix. The bare-`%` forms are kept from the round-4
    ladder so its committed behaviour is unchanged.
    """
    lower, upper = format(ord(letter), "x"), format(ord(letter.upper()), "x")
    assert lower[1] == upper[1], letter
    high = _html_or_literal(lower[0] + upper[0])
    low = _html_or_literal(lower[1])
    complete = (
        "(?:"
        + "|".join(
            [
                letter,
                *_html_numeric_refs(ord(letter)),
                *_html_numeric_refs(ord(letter.upper())),
                _PERCENT_SIGN_RE + high + low,
            ]
        )
        + ")"
    )
    partial = (
        "(?:"
        + "|".join(
            [
                complete,
                _PERCENT_SIGN_RE + high,
                _PERCENT_SIGN_RE,
                rf"%[{lower[0]}{upper[0]}]?",
            ]
        )
        + ")"
    )
    return complete, partial


_TOKEN_PARAM_NAME_SPELLINGS = tuple(_token_name_letter(c) for c in "token")
_TOKEN_PARAM_NAME_RE = "".join(c for c, _ in _TOKEN_PARAM_NAME_SPELLINGS)
_TOKEN_PARAM_NAME_PREFIX_RE = (
    "(?:"
    + "|".join(
        "".join(c for c, _ in _TOKEN_PARAM_NAME_SPELLINGS[:k]) + _TOKEN_PARAM_NAME_SPELLINGS[k][1]
        for k in range(len(_TOKEN_PARAM_NAME_SPELLINGS))
    )
    + ")"
)
_TOKEN_PARAM_RE = re.compile(
    rf"{_TOKEN_PARAM_SEP_RE}(?ai:{_TOKEN_PARAM_NAME_RE}){_TOKEN_PARAM_EQ_RE}(?!\$\{{)"
    + "("
    + _keyed_value_group(_TOKEN_PARAM_VALUE_CLASS)
    + ")"
)

# The in-progress form of the same anchor, for `StreamRedactor.feed`'s
# credential-anchored holdback escalation (mirrors `_BEARER_ANCHOR_PARTIAL_RE`).
# `?` `&` `=` are all in `_CRED_CLASS`, so a token URL is one withheld run --
# but a run longer than the 512-char DoS floor with NO recognised credential
# anchor is BISECTED, and for a >=512-char opaque value the bisection point
# lands inside the value: the committed prefix carries the `token=` anchor
# (and is redacted), while the tail reaches `flush()` anchor-less and streams
# raw. Recognising the trailing partial escalates the tail to the 4096
# ceiling and the fail-closed drop past it, exactly like a Bearer token.
#
# Every alternative below is one possible end-of-chunk prefix. For a percent
# spelling, `%(?:[57]4?)?` (and its siblings) includes the bare `%`, the first
# hex nibble, and the complete escape. The surrounding scoped `(?ai:...)` folds
# both literal letters and hex letters without admitting Unicode lookalikes.
# The same generated entity composition supplies complete-or-partial spellings
# at every letter boundary.
# `*` (not `+`): a buffer ending at a complete separator/name/equals spelling is
# already an in-progress value match. A mid-entity tail needs no extra
# alternative: every byte of `&amp` / `&#x2` belongs to `_CRED_CLASS`, while the
# terminating `;` does not. The explicit entity-only alternative holds the
# completed spelling at buffer end before that semicolon can release it.
#
# The value alternation is the batch grammar's (`_keyed_value_group`, with `*`
# on the plain run): a registered tag standing at the value's head, then any
# value bytes. Without the tag atom, the tag's interior space ends the class
# run and a tail such as `?token=[REDACTED: credential]` -- or the same tag
# with `!` or a secret's first bytes glued to its `]` -- is not an in-progress
# anchor at all: `StreamRedactor.feed` commits anchor and tag (the batch pass
# leaves a whole tag standing) and the next chunk's glued bytes arrive
# anchor-less, where nothing redacts them. With it, such a tail is held as a
# STRONG anchor until a terminator, exactly like `?token=<opaque>`.
#
# A STRICT PREFIX of a tag is the third alternative, for the boundary that falls
# INSIDE the tag. Before the interior space the prefix is a class run and the
# second alternative already holds it; at or after that space (`?token=[REDACTED:
# `, `?token=[REDACTED: cred`) neither of the other two reaches `\Z`, so the
# tail was no anchor: the canonical-tag hold in `feed` pulled the cut back only
# to the `[`, `?token=` was committed, and the next chunk's completed tag with
# the secret glued to its `]` arrived anchor-less -- the shape the atom above
# exists for, reached through a boundary one byte later. The prefix is a STRONG
# anchor (the `eq` group matched) held from `token=`; a strict prefix is at most
# one byte short of a tag, so the hold is bounded by the longest registered tag
# and can neither raise a cap by itself nor leave a drop armed on text that
# turns out to be a bare tag: the next chunk either completes the tag (released
# with its ordinary tail by the batch pass, or redacted whole with glued bytes)
# or breaks the prefix (an ordinary value, redacted as ever). The prefix may
# follow a run of whole tags (`?token=[REDACTED: credential][REDACTED: `): the
# tag atom is a run, so the boundary inside the SECOND tag of a value is held
# exactly as the boundary inside the first.
_TOKEN_PARAM_PARTIAL_RE = re.compile(
    rf"(?:{_TOKEN_PARAM_SEP_RE}(?:(?ai:{_TOKEN_PARAM_NAME_PREFIX_RE})"
    rf"|(?ai:{_TOKEN_PARAM_NAME_RE})(?P<eq>{_TOKEN_PARAM_EQ_RE})"
    rf"(?:{_CREDENTIAL_TAG_ATOM}{_TOKEN_PARAM_VALUE_CLASS}*"
    rf"|(?:{_CREDENTIAL_TAG_ATOM})?{_CREDENTIAL_TAG_PREFIX_ATOM}"
    rf"|{_TOKEN_PARAM_VALUE_CLASS}*))"
    rf"|{_TOKEN_PARAM_SEP_ENTITY_RE})\Z"
)


#: One redaction the batch redactor has decided on, positioned against the
#: ORIGINAL text: ``(start, end, replacement)``. Every pass produces these and
#: nothing is written until every pass has spoken.
_RedactionSpan = tuple[int, int, str]


def _span_end(span: _RedactionSpan) -> int:
    return span[1]


def _uncovered(start: int, end: int, taken: list[_RedactionSpan]) -> list[tuple[int, int]]:
    """Return the parts of ``[start, end)`` that no span in *taken* covers.

    *taken* must be sorted and pairwise disjoint, which is how
    :func:`redact_credentials` builds it, so the spans are ordered by ``end`` as
    well as by ``start`` and one bisect on ``end`` lands on the first span that
    can still reach into ``[start, end)``. From there a forward walk over the
    spans that begin before ``end`` yields each gap between them.
    """
    gaps: list[tuple[int, int]] = []
    cursor = start
    i = bisect.bisect_right(taken, start, key=_span_end)
    while i < len(taken) and taken[i][0] < end:
        if taken[i][0] > cursor:
            gaps.append((cursor, taken[i][0]))
        cursor = max(cursor, taken[i][1])
        i += 1
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


@functools.lru_cache(maxsize=1)
def _host_darwin_user_dir_id() -> str | None:
    """This user's ``<2>/<30>`` directory id as the OS reports it, asked once.

    ``confstr``, not ``tempfile.gettempdir()``, which follows ``$TMPDIR``. Windows
    has no ``os.confstr`` and other POSIX systems refuse the name; either, or an
    answer outside the grammar, means None and nothing withheld.
    """
    confstr = getattr(os, "confstr", None)
    if confstr is None:
        return None
    try:
        match = _DARWIN_USER_TEMP_DIR_RE.match(confstr(_CS_DARWIN_USER_TEMP_DIR) or "")
    except (OSError, ValueError):
        return None
    return match.group("id") if match else None


@functools.lru_cache(maxsize=4)
def _darwin_user_dir_id_re(host_id: str) -> re.Pattern[str]:
    return re.compile(
        _DARWIN_USER_DIR_PREFIX_RE + f"(?P<id>{re.escape(host_id)})" + _DARWIN_USER_DIR_SUFFIX_RE
    )


def _darwin_user_dir_ids(text: str) -> list[_RedactionSpan]:
    """This host's directory id in *text*, as spans for :func:`_uncovered` to subtract."""
    host_id = _host_darwin_user_dir_id()
    # The substring check keeps the regex off the almost-every text that does not
    # carry this host's id.
    if host_id is None or f"folders/{host_id}/" not in text:
        return []
    return [(*m.span("id"), "") for m in _darwin_user_dir_id_re(host_id).finditer(text)]


def _covering_claims(start: int, end: int, taken: list[_RedactionSpan]) -> int:
    """How many spans in *taken* overlap ``[start, end)`` -- the same bisect as
    :func:`_uncovered`, for a value with no gap: one claim means the value is
    already one tag, two or more mean two adjacent tags that must coalesce."""
    count = 0
    i = bisect.bisect_right(taken, start, key=_span_end)
    while i < len(taken) and taken[i][0] < end:
        count += 1
        i += 1
    return count


def _splice(text: str, spans: list[_RedactionSpan]) -> str:
    """Apply *spans* (sorted, disjoint) to *text* in one left-to-right pass."""
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in spans:
        parts.append(text[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _credential_value_span(match: "re.Match[str]") -> tuple[int, int]:
    """The span pass 1 redacts for one ``_CREDENTIAL_PATTERNS`` match.

    A key-anchored branch -- one that begins at the key naming the secret --
    exposes the secret VALUE as its one capturing group, and only that group is
    redacted: the key, the ``:``/``=`` separator and the quotes around the value
    stay, so a redacted JSON / YAML / INI / ``.env`` / header-line document keeps
    its structure and a JSON document still parses. A branch with no capturing
    group IS the secret and is redacted whole.

    ``Match.lastindex`` is the one group the matched branch closed (the
    alternation's branches are mutually exclusive and each carries at most one
    group, which ``test_redaction_key_anchored_value_span.py`` pins), so this is
    one rule for every branch with no per-branch case.
    """
    if match.lastindex is None:
        return match.span()
    return match.span(match.lastindex)


def redact_credentials(text: str) -> tuple[str, list[str]]:
    """Redact raw credential patterns from text, including base64-encoded.

    Returns (cleaned_text, list_of_warnings).
    """
    spans, warnings, _rules = _credential_redaction_plan(text)
    if not spans:
        return text, warnings
    return _splice(text, spans), warnings


#: Label forms of the key-value AWS branches: the key name, its separator and
#: an optional opening quote. The redactor keeps the label in the text and
#: replaces only the value after it, so a record carries the label to let the
#: reader see which field the removed value belonged to without re-reading the
#: cleaned text. A label is a fixed key name, never secret.
_AWS_LABEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:SecretAccessKey|aws_secret_access_key)"
            + _LABEL_QUOTE
            + r"\s*[:=]\s*"
            + _LABEL_QUOTE
        ),
        "aws_secret_access_key",
    ),
    (
        re.compile(
            r"(?:SessionToken|aws_session_token)" + _LABEL_QUOTE + r"\s*[:=]\s*" + _LABEL_QUOTE
        ),
        "aws_session_token",
    ),
    (
        re.compile(
            r"(?:AccessKeyId|aws_access_key_id)" + _LABEL_QUOTE + r"\s*[:=]\s*" + _LABEL_QUOTE
        ),
        "aws_access_key_id",
    ),
)

#: Unlabelled pass-1 branches, in the alternation's order, each with the rule
#: id a record names. The first whose pattern fully matches the redacted span
#: wins; a span none of them matches is ``credential_pattern``.
_PASS1_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(AWS_KEY_ID), "aws_access_key_id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*"), "private_key"),
    (re.compile(r"xox[bpas]-[\s\S]*"), "slack_token"),
    (re.compile(r"[0-9]{6,}:[A-Za-z0-9_-]{30,}"), "telegram_bot_token"),
    (
        re.compile(r"[MNO][A-Za-z0-9_-]{22,30}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}"),
        "discord_bot_token",
    ),
    (re.compile(r"(?:gh[opsur]_|github_pat_)[\s\S]*"), "github_token"),
    (re.compile(r"glpat-[\s\S]*"), "gitlab_token"),
    (re.compile(r"(?:sk|rk)_(?:live|test)_[\s\S]*"), "stripe_key"),
    (re.compile(r"SG\.[\s\S]*"), "sendgrid_key"),
    (re.compile(r"sk-proj-[\s\S]*"), "openai_key"),
    (re.compile(r"sk-ant-[\s\S]*"), "anthropic_key"),
    (re.compile(r"npm_[\s\S]*"), "npm_token"),
    (re.compile(r"pypi-[\s\S]*"), "pypi_token"),
    (re.compile(r"do[opr]_v1_[\s\S]*"), "digitalocean_token"),
    (re.compile(r"GOCSPX-[\s\S]*"), "google_oauth_secret"),
    (re.compile(r"[a-z+]+://[^\s:/@]*:[^\s/]+@"), "url_userinfo"),
    (re.compile(r"eyJ[\s\S]*"), "jwt"),
)


def _pass1_rule(matched: str) -> tuple[str, str]:
    """``(rule_id, label)`` for one pass-1 match; ``label`` is ``""`` when none."""
    for pattern, rule in _AWS_LABEL_RULES:
        head = pattern.match(matched)
        if head is not None:
            return rule, head.group()
    for pattern, rule in _PASS1_RULES:
        if pattern.fullmatch(matched):
            return rule, ""
    return "credential_pattern", ""


class CredentialMatch(NamedTuple):
    """One credential placeholder the redactor wrote, described for a record.

    ``ordinal`` is the placeholder's index among EVERY credential tag in the
    cleaned text (tags already present in the input count too), which is how a
    renderer pairs the record with the tag it describes. ``value`` is the
    removed plaintext: it exists so the caller can look for where the value
    came from while the turn is still in memory, and it must never be stored,
    logged or sent anywhere.
    """

    ordinal: int
    rule: str
    label: str
    value: str


def redact_credentials_with_records(text: str) -> tuple[str, list[str], list[CredentialMatch]]:
    """:func:`redact_credentials`, plus one :class:`CredentialMatch` per tag written.

    The cleaned text and warnings are byte-identical to ``redact_credentials``;
    both share one plan, so the records cannot describe a redaction the text
    does not contain.
    """
    spans, warnings, rules = _credential_redaction_plan(text)
    if not spans:
        return text, warnings, []
    matches: list[CredentialMatch] = []
    ordinal = 0
    cursor = 0
    for start, end, tag in spans:
        ordinal += sum(text.count(t, cursor, start) for t in CREDENTIAL_REDACTION_TAGS)
        rule, label = rules.get(start, ("credential_pattern", ""))
        # The span IS the removed plaintext: a key-anchored branch's label stays
        # in the cleaned text ahead of the span, so none of it is inside.
        matches.append(CredentialMatch(ordinal, rule, label, text[start:end]))
        ordinal += 1
        cursor = end
    return _splice(text, spans), warnings, matches


def _credential_redaction_plan(
    text: str,
) -> tuple[list[_RedactionSpan], list[str], dict[int, tuple[str, str]]]:
    """Every span :func:`redact_credentials` rewrites, with its warnings and rules.

    Returns ``(spans, warnings, rules)``: ``spans`` sorted and disjoint against
    the input, ``rules`` mapping each span's start to its ``(rule_id, label)``.

    Unconditional on every surface EXCEPT inside an explicit
    ``redaction_switch.owner_view()`` scope: there, and only there, the owner's
    switch is consulted, and ``enabled: false`` returns ``text`` unchanged with
    no warnings. A caller that has not opened the scope -- every channel egress,
    every admission gate, every log -- gets the full pass whatever the owner
    chose (see the ``redaction_switch`` module docstring for the seams that do
    open it).

    Every pass positions its redactions as spans against the IMMUTABLE input,
    and the string is rewritten exactly once at the end. Redacting by matched
    VALUE (``result.replace(matched, tag, 1)``) rewrites the first textual
    occurrence of the value, which is not necessarily the span that matched:
    when a later match also occurs as a substring of an earlier, longer run
    that is NOT itself redacted, the tag lands inside the innocent host and the
    real standalone credential survives in plaintext. Splicing by span makes
    that unreachable in all three passes.

    Passes are ranked: pass 1 outranks pass 2 outranks pass 3 outranks pass 4.
    A later pass's span never rewrites text an earlier pass already claimed; it
    redacts only the part of its span still standing in plaintext, so no
    character is redacted twice and no character a pass flagged is left behind.
    """
    if credential_pass_bypassed():
        return [], [], {}

    warnings: list[str] = []
    rules: dict[int, tuple[str, str]] = {}

    # 1. Plaintext credential patterns.
    #
    # Gated on the cheap superset pre-filter: when no branch of
    # `_CREDENTIAL_PATTERNS` can possibly match, `finditer` would yield nothing
    # and the loop body would not run, so skipping it cannot change the output.
    # This is the hot path — the alternation is 23 branches retried at nearly
    # every position, and real text almost never contains a credential.
    #
    # `taken` is every span an earlier pass has claimed, kept sorted and
    # disjoint; it is what the later passes subtract from.
    taken: list[_RedactionSpan] = []
    # End of the last pass-1 claim; only a quoted claim can reach past its own
    # match. A later match that reach covers whole is skipped, and one that
    # straddles its end is clamped to the part past it (see below).
    claimed_until = 0
    scans = _KeyedValueScans()
    if _might_contain_credential(text):
        for m in _credential_matches(text):
            # Emit ONLY non-sensitive metadata (length). Do NOT slice any part of
            # the match into the warning: `_CREDENTIAL_PATTERNS` matches the raw
            # secret value itself (e.g. `ghp_…`, `sk-ant-…`), so even a short prefix
            # is genuine plaintext key material — a fixed-length token prefix leaves
            # ~12-16 secret chars in a 20-char slice. The warnings list is a
            # redaction-subsystem output expected to be safe to log/surface, so it
            # must carry no secret bytes. The base64 / bare-secret passes below
            # likewise log length only.
            #
            # The redacted span is the branch's VALUE group when it has one (a
            # key-anchored branch keeps the key, separator and quotes), else the
            # whole match; the length reported is the length redacted.
            #
            # Inside a QUOTED value the boundary is the closing quote, not the
            # class's stop: the quoted string is one value, so the claim runs to
            # the first unescaped closing quote on the line -- or to the line's
            # end when the quote never closes (`scan_keyed_value`) -- on the
            # FIRST claim.
            #
            # A value that is already one of this module's fixed credential tags
            # is skipped, not re-redacted (`_value_is_credential_tag`): once
            # the key survives, a second run over `key = [REDACTED: credential]`
            # sees the pair again, and the value class stops at the tag's interior
            # space, so claiming it would mangle the tag on every re-redacting
            # surface. The skip judges the SAME extended span, so a tag that
            # fills its quoted value is skipped while a tag heading a quoted
            # value that continues is claimed through the quote and warned. Only
            # a key-anchored branch can meet a tag here -- a whole-match branch
            # IS its secret's shape and never matches one.
            #
            # The skip needs the tag to demonstrably FILL the value: unquoted,
            # the class boundary after it says so; quoted, the closing quote
            # does. A quote that never closes on its line certifies nothing (the
            # scan ends at the raw break or the text's end), and a quoted string
            # folds across a raw break in YAML and the shell, so a tag that ends
            # such a line may head a value that goes on below it -- an author
            # can write that tag, and a skip there silences the one signal
            # `decisions.gate.scrub_reason` has for the pair while the
            # continuation stands unwarned. The bytes ARE the tag, so nothing is
            # rewritten and no record is written (`CredentialMatch` describes a
            # tag the redactor wrote); the line is warned, on every run. The
            # scan stays line-bounded: reaching into the next line would let one
            # unterminated quote swallow the rest of a document into one tag.
            #
            # The redactor never leaves that shape behind itself: a claim that
            # runs to the end of an unterminated line WRITES the closing quote
            # the line never had (`key="<s1> <s2>` reads back as
            # `key="[REDACTED: credential]"`), so its own output is a tag that
            # fills a closed quoted value and a re-screen of persisted history,
            # an artifact or an auto-nudge state is silent, where leaving the
            # quote open would have every consumer that gates on the warning
            # list refuse text the redactor itself wrote, with nothing in the
            # product to clear it. The written quote is the one byte that tells
            # a judged pair from an author-written tag: the pair was judged
            # once, with everything this pass has, and the bytes past the line
            # break were judged on their own in the same run.
            value = scans.value_of(text, m)
            if value is None:
                start, end = m.span()
            else:
                if value.end <= value.start:
                    continue  # a key with no value: nothing to claim
                start, end = value.start, value.end
            if end <= claimed_until:
                # Covered whole by an earlier claim: those bytes are already
                # redacted, and a second span there would overlap it.
                continue
            quote_closes = True
            closing_quote = ""
            if start < claimed_until:
                # Straddles the claim's end. A whole-match class admits a quote
                # byte (a connection URI's userinfo, a PEM body), so a match can
                # begin inside the claim and end past it; the part past the claim
                # is still plaintext and is this match's claim. Skipping the
                # whole match would leave that tail -- the URI's password, the
                # PEM body -- standing with no warning.
                start = claimed_until
            elif value is not None:
                # The scanner's answer: the claim runs to the closing quote on
                # the value's line, or to the line's end when the quote never
                # closes -- and then the close is WRITTEN, the opener as it was
                # spelled (`"` or `\"`), so an embedded pair closes inside its
                # enclosing literal.
                quote_closes = value.closes
                if not quote_closes:
                    closing_quote = value.opener
            if _value_is_credential_tag(text, start, end):
                if quote_closes:
                    continue
                claimed_until = end
                warnings.append(f"Redacted credential pattern ({end - start} chars)")
                continue
            claimed_until = end
            warnings.append(f"Redacted credential pattern ({end - start} chars)")
            taken.append((start, end, _REDACTED_CREDENTIAL_TAG + closing_quote))
            # Keyed at the span the record describes -- the value's start, not
            # the match's: a key-anchored branch's key is text the redactor
            # keeps, so it is outside the span. The rule is still read from the
            # whole match, which is where the label lives.
            rules[start] = _pass1_rule(m.group())

    # Passes 2 and 3 both scan the ORIGINAL `text` for runs of the base64
    # alphabet, and they select the SAME spans: `[A-Za-z0-9+/]{40,}` is greedy and
    # leftmost, so it yields exactly the maximal runs of length >= 40 — which is
    # also precisely what `_BARE_SECRET_RUN_RE`'s `(?<![A-Za-z0-9+/])` /
    # `(?![A-Za-z0-9+/])` boundaries select. The only difference is the trailing
    # `={0,2}` padding that `_B64_CHUNK_RE` additionally consumes, and `=` is not
    # in the run's character class, so `rstrip("=")` recovers the bare run
    # exactly. So one scan feeds both passes instead of two.
    #
    # The two loops stay SEPARATE and in their original order: `warnings` is a
    # contract (all pass-2 warnings precede all pass-3 warnings), and pass 3
    # subtracts every pass-2 claim, so pass 2 must have finished first.
    b64_matches = list(_B64_CHUNK_RE.finditer(text))

    # 2. Base64-encoded credentials.
    #
    # The warning is emitted for every chunk that decodes to a credential, even
    # one that pass 1 already claimed in full: the encoded credential IS
    # redacted, and the warning counts credentials found, not splices made.
    pass2: list[_RedactionSpan] = []
    for m in b64_matches:
        chunk = m.group()
        if not _decode_b64_chunk(chunk):
            continue
        warnings.append(f"Redacted base64-encoded credential ({len(chunk)} chars)")
        for start, end in _uncovered(m.start(), m.end(), taken):
            pass2.append((start, end, _REDACTED_ENCODED_CREDENTIAL_TAG))
            rules[start] = ("encoded_credential", "")
    # Pass-2 chunks are disjoint from each other and were cut around `taken`,
    # so the union is disjoint and a sort restores the order.
    taken = sorted(taken + pass2)

    # 3. BARE 40-char AWS secret keys with no label/prefix. These carry no
    # distinctive marker for _CREDENTIAL_PATTERNS to anchor on, so an entropy +
    # structural heuristic is the only way to catch a standalone secret value.
    #
    # A run an earlier pass has claimed in full (it was a labelled value, or an
    # encoded-credential chunk) is skipped WITHOUT a warning. The check is
    # positional: a second occurrence of the same run elsewhere in the text is
    # judged on its own span, never on whether the value still appears
    # somewhere. A run only PARTLY claimed — a glued key whose tail is the first
    # word of a `aws_secret_access_key=` label, say — has the part still in
    # plaintext redacted, because the run as a whole was judged to hold a key
    # and the earlier pass consumed only its label.
    #
    # Only windows sharing >= _HOST_ID_EXEMPT_OVERLAP bytes with this host's id are exempt;
    # whole-run context preserves every other base verdict, including smaller overlaps.
    withheld = _darwin_user_dir_ids(text)
    pass3: list[_RedactionSpan] = []
    link_spans: list[tuple[int, int]] | None = None
    for m in b64_matches:
        run_end = m.start() + len(m.group().rstrip("="))
        run = text[m.start() : run_end]
        pieces = _uncovered(m.start(), run_end, withheld)
        if pieces == [(m.start(), run_end)]:
            if not _contains_bare_secret(run):
                continue
        else:
            first = bisect.bisect_right(withheld, m.start(), key=_span_end)
            last = bisect.bisect_left(withheld, run_end, key=lambda span: span[0])
            skip_spans = tuple(
                (start - m.start(), end - m.start()) for start, end, _ in withheld[first:last]
            )
            # A non-skipped positive window overlaps the id by < the exempt
            # bound, so it falls wholly inside one piece or spans a piece
            # boundary where the id sits; either way it keys every piece it
            # touches. Windows ascend, so per piece advance past windows ending
            # at or before its start, then it is keyed if the current window
            # starts before its end.
            windows = _bare_secret_window_starts(run, skip_spans)
            hit = next(windows, None)
            keyed_pieces = []
            for start, end in pieces:
                while hit is not None and m.start() + hit + _SECRET_KEY_LEN <= start:
                    hit = next(windows, None)
                if hit is not None and m.start() + hit < end:
                    keyed_pieces.append((start, end))
            pieces = keyed_pieces
            if not pieces:
                continue
        # A run wholly inside a validated document link is its host and route,
        # not a key (see `_DOCUMENT_LINK_RE`). Scanned only after a run fires,
        # so text with no key-shaped run never pays for it.
        if link_spans is None:
            link_spans = _document_link_spans(text)
        if any(start <= m.start() and run_end <= end for start, end in link_spans):
            continue
        for piece_start, piece_end in pieces:
            piece = text[piece_start:piece_end]
            gaps = _uncovered(piece_start, piece_end, taken)
            if not gaps:
                continue
            for start, end in gaps:
                pass3.append((start, end, _REDACTED_CREDENTIAL_TAG))
                rules[start] = ("bare_aws_secret", "")
            warnings.append(f"Redacted bare secret key ({len(piece)} chars)")
    taken = sorted(taken + pass3)

    # 4. `?token=` / `&token=` URL parameter VALUES, keyed on the parameter
    # name (see `_TOKEN_PARAM_RE`). Ranked LAST so a value an earlier pass
    # already caught -- an AKIA key, a JWT, a link token -- keeps that pass's
    # tag, warning and span byte-identically; this pass only claims the opaque
    # values nothing shape-based can see.
    #
    # UNGATED, deliberately: the name folds case (`(?i:token)`), and a
    # case-insensitive pattern is only safely anchored by the SAME regex
    # engine (see `_CREDENTIAL_PREFILTER_AUTHORIZATION_RE` for the
    # `str.lower()` bypass this rule exists to prevent) -- so the cheapest
    # valid gate is a same-engine search whose cost equals the scan it would
    # skip, which is no gate at all. This pass is one two-alternation-free
    # regex, not the 23-branch alternation pass 1's pre-filter exists for.
    #
    # A value that is already one of this module's fixed credential tags is
    # skipped, not re-redacted -- the same `_value_is_credential_tag` rule
    # pass 1 applies to a key-anchored branch's value, and for the same reason:
    # several surfaces run the redactor twice, and the value class stops at a
    # tag's interior space, so matching a canonical tag again would mangle
    # `token=[REDACTED: credential]` into
    # `token=[REDACTED: credential] credential]` on the second run.
    #
    # Trust is BYTE-IDENTITY with a module-owned fixed literal, never a shape.
    # `_TOKEN_PARAM_VALUE_CLASS` admits `[`, `]` and `:`, so a prefix test lets
    # adversary-authored `?token=[REDACTED<secret>` bypass this terminal pass.
    # `CREDENTIAL_REDACTION_TAGS` is the key because it contains ONLY fixed
    # literals. The exfiltration prefix is excluded for exactly that reason:
    # skipping a domain-bounded exfil shape is the same bypass --
    # `?token=[REDACTED: suspicious URL to <secret>.co]` satisfies the domain
    # class while carrying attacker-controlled bytes.
    #
    # ACCEPTED RESIDUAL: a genuine exfil tag value is redacted at its 10-byte
    # `[REDACTED:` head, yielding
    # `?token=[REDACTED: credential] suspicious URL to <domain>]`. That text is
    # already redacted and contains no secret; it is stable on re-redaction
    # because the second pass sees the exact credential literal and skips, and
    # the bare domain tail cannot re-trigger the exfil pass (`_URL_RE` requires
    # a scheme). One notice count moves from exfil to credential.
    #
    # A value an earlier pass claimed only PARTLY is ONE tag, not a tag per
    # gap. Since a key-anchored branch keeps its key, a value such as
    # `aws_secret_access_key=<secret>` is claimed by pass 1 only from the
    # secret on, and tagging the leftover `aws_secret_access_key=` gap on its
    # own would write two adjacent tags -- which is not a fixed point: on the
    # next run over that output the value `[REDACTED: credential][REDACTED:
    # credential]` is not byte-identical to a tag, the class stops at the
    # interior space, and the head `[REDACTED: credential][REDACTED:` is
    # claimed as a value, leaving `?token=[REDACTED: credential] credential]`
    # on every re-redacting surface. So the partly-covered value and every
    # claim it overlaps are replaced by one span carrying this pass's tag, the
    # same text a whole-match claim of the pair produced before the key
    # survived. The merged span goes into `taken`, not `pass4`: a claim that
    # reaches past the value (the AWS value class runs to whitespace, so it
    # can swallow `&next=…`) may cover a later parameter's value, which must
    # then read as claimed rather than be tagged a second time.
    # `taken` is rebuilt in ONE forward sweep alongside the ordered matches:
    # every earlier claim moves into `swept` exactly once, coalesced where a
    # partly-covered value overlaps it. The bound is O(M log C + C) for M
    # matches over C claims -- each match's `_uncovered` is a bisect into the
    # sorted claims, and the sweep visits each claim once -- not the O(M * C)
    # a per-match rescan and rebuild of `taken` costs, which would let N
    # nested pairs in one text event take O(N^2) on the event loop that runs
    # this function synchronously (measured 4x per doubling, 8.9 s at N = 12000).
    pass4: list[_RedactionSpan] = []
    swept: list[_RedactionSpan] = []
    t = 0
    for m in _TOKEN_PARAM_RE.finditer(text):
        value_start, value_end = m.start(1), m.end(1)
        if _value_is_credential_tag(text, value_start, value_end):
            continue
        gaps = _uncovered(value_start, value_end, taken)
        if not gaps and _covering_claims(value_start, value_end, taken) < 2:
            # One earlier claim covers the value (or reaches past it): the
            # output already reads as one tag there.
            continue
        # A value with NO gap but TWO or more claims across it -- two
        # credentials adjacent inside one value (`?token=AKIA…AKIA…`) -- would
        # otherwise come out as two adjacent tags, which the next run reads as
        # one value whose head stops at the second tag's interior space and
        # mangles, with a warning about text that holds no secret. It is
        # coalesced like a partly-covered value, but silently: every byte of it
        # was already claimed and warned for by the pass that found it.
        while t < len(taken) and taken[t][1] <= value_start:
            swept.append(taken[t])
            t += 1
        if gaps == [(value_start, value_end)]:
            pass4.append((value_start, value_end, _REDACTED_CREDENTIAL_TAG))
            rules[value_start] = ("token_parameter", "")
        else:
            start, end = value_start, value_end
            # The span coalesced for the previous parameter reaches into this
            # value when one claim runs through both (the AWS value class stops
            # at `,` and `}`, where this class does not): it is extended, not
            # duplicated.
            if swept and swept[-1][1] > value_start:
                previous = swept.pop()
                rules.pop(previous[0], None)
                start, end = min(start, previous[0]), max(end, previous[1])
            # Every claim that begins before the growing end overlaps the
            # merged span; a claim beginning past it would overlap the one that
            # extended it, which disjointness rules out.
            while t < len(taken) and taken[t][0] < end:
                rules.pop(taken[t][0], None)
                start, end = min(start, taken[t][0]), max(end, taken[t][1])
                t += 1
            swept.append((start, end, _REDACTED_CREDENTIAL_TAG))
            # One span, one record: a claim the merged span absorbs is not a
            # span of its own, so its rule goes with it.
            rules[start] = ("token_parameter", "")
        if gaps:
            warnings.append(f"Redacted token parameter value ({value_end - value_start} chars)")
    taken = swept + taken[t:]

    return sorted(taken + pass4), warnings, rules


# Absolute filesystem paths, POSIX and Windows. Deliberately narrow: anchored to
# real filesystem roots rather than "any slash-separated token", and both branches
# refuse to start mid-token so a URL is never mistaken for a path -- without the
# lookbehinds, ``https://api.github.com/repos/x`` matches twice (``s:/`` as a drive
# letter, ``/repos`` as a root) and the URL is destroyed.
#
# The drive-letter branch accepts BOTH separators: ``C:\`` and ``C:/`` name the
# same file on Windows, and tools that normalise separators (Git Bash, Python's
# pathlib/posixpath, Node, MSYS) routinely print the forward-slash spelling, so
# matching only ``C:\`` left ``C:/Users/<login>/...`` -- the login and host
# layout -- unredacted wherever this shared scrub runs. The forward-slash form
# carries a ``(?!/)`` guard so a one-letter URI scheme (``x://host``) is never
# mistaken for a drive; longer schemes (``https:``) are already refused by the
# ``(?<![A-Za-z])`` lookbehind on the drive letter itself.
_LOCAL_PATH_RE = re.compile(
    r"(?:"
    r"(?<![\w:/])/(?:local/home|home|Users|root|tmp|var|opt|usr|etc|private|mnt|srv|workspace|workplace)"
    r"|(?<![A-Za-z])[A-Za-z]:(?:\\|/(?!/))"
    r")"
    r"[^\s'\"<>|]*"
)
_LOCAL_PATH_PLACEHOLDER = "[redacted-path]"


def redact_local_paths(text: str) -> tuple[str, list[str]]:
    """Strip absolute host filesystem paths from *text*.

    Complements :func:`redact_credentials`, which matches credential *patterns*
    and leaves a bare path such as
    ``[Errno 2] No such file or directory: '/home/alice/.kiro/crew/vaults/v1'``
    untouched. That string is the common shape of an OS or subprocess error, and
    on an error surface that reaches a browser it discloses the account name and
    on-disk layout of the host (CWE-209).

    Returns the redacted text and a list of human-readable notes, matching the
    signature of the sibling passes so callers can chain them uniformly.
    """
    notes: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        notes.append(f"Redacted local path ({len(match.group(0))} chars)")
        return _LOCAL_PATH_PLACEHOLDER

    return _LOCAL_PATH_RE.sub(_sub, text), notes


#: A random key generated once per gateway process and held only in memory: it
#: is never persisted, logged or exposed. It keys the per-segment label below.
_PATH_LABEL_KEY = secrets.token_bytes(32)

#: Joins a redacted path segment to the label :func:`redact_path_segments`
#: appends. Outside the credential alphabet, so the label can never be glued
#: onto a neighbouring run and read as part of one; and not a path separator, so
#: the segment count is kept.
_PATH_SEGMENT_DISCRIMINATOR_SEP = "~"


def _path_segment_label(segment: str) -> str:
    """The opaque, process-stable label for a redacted path *segment*.

    ``HMAC-SHA256(_PATH_LABEL_KEY, segment)``, truncated to 12 hex digits (48
    bits). Equal segments carry equal labels for the life of this process, and
    that equality is the point: the dashboard joins the project-tree response
    with the git-status response by path, so the same original must label the
    same way in both. Distinct segments carry distinct labels except with
    negligible probability (48 bits over the handful of collisions one tree can
    hold). The digest is KEYED: without the key nothing about the segment can be
    checked against the label, so a low-entropy secret behind a redaction tag is
    not exposed to an offline dictionary guess the way an unkeyed hash prefix
    would be. The key is fresh per gateway process, so the label changes across
    a restart; both responses of one join come from one process, so the join
    holds.
    """
    return hmac.new(
        _PATH_LABEL_KEY, segment.encode("utf-8", "surrogatepass"), hashlib.sha256
    ).hexdigest()[:12]


def redact_path_segments(path: str, redactor: Callable[[str], str] | None = None) -> str:
    """Redact a ``/``-separated *path* segment by segment, labelling each
    redacted segment so distinct originals stay distinct.

    The whole-string redactors replace a matched token wherever it sits, so a
    path whose filename is credential-shaped (``AKIA…_model.txt``) keeps its
    directory prefix and its non-secret tail but not the token. Here each segment
    is redacted on its own, so a credential-shaped segment is replaced by the tag
    while every clean segment around it is kept verbatim. Every segment the
    redactor changes is then suffixed with :data:`_PATH_SEGMENT_DISCRIMINATOR_SEP`
    and :func:`_path_segment_label` of its ORIGINAL bytes -- not only on a
    collision, because a single call cannot know what else the listing holds.

    The label is the one shape that meets all five properties the listings need
    at once:

    1. Distinct inputs stay distinct: two different credential-shaped segments
       collapse to the same tag but carry different labels, so a de-duplicating
       listing keeps both.
    2. No byte of the secret is in the output: the tag replaces the token whole
       and the label is a digest, not a substring.
    3. No UNKEYED digest of the secret: the label is an HMAC under a per-process
       random key, so a reader cannot enumerate low-entropy candidates offline
       and match them against the label.
    4. No dependence on listing position or order: the label is a function of
       the segment alone, so a sorted listing does not correlate the label with
       the secret's lexicographic rank, and the same path labels the same way
       whatever else is listed with it.
    5. Stable across responses within one gateway process: the dashboard joins
       the tree response with the git-status response by path, and a
       per-response label breaks that join when only one of two colliding paths
       appears in the status response. A keyed label is the same in every
       response this process serves.

    The key is regenerated when the gateway restarts, so labels differ across
    restarts; both responses of one join come from the same process, so that
    is fine.

    *redactor* is the whole-string redactor to apply -- callers on an egress
    surface pass the context-aware ``redact`` shim so a loaded companion's extra
    patterns apply; the default is the credential pass alone. Whatever it is,
    this function never emits LESS redaction than it would: the segment-wise
    result is returned only when redacting each segment on its own removes
    EXACTLY the bytes the whole-string pass removes (their unlabelled joins are
    equal) and the labelled result is itself a fixed point of the redactor;
    otherwise the whole-string result is returned unchanged. Equality with the
    whole-string pass is the load-bearing check: a token that spans a separator
    (a ``key=value`` whose value carries a ``/``) is matched by the whole pass
    but only up to the separator by the segment pass, and the leftover tail is
    not a match on its own, so a fixed-point check alone would let it through.
    A path the redactor leaves alone is returned as is. Splits on
    :data:`posixpath.sep` only, on every host: the project listings this serves
    emit POSIX-relative paths, not native ones.
    """
    _redact: Callable[[str], str] = redactor or (lambda s: redact_credentials(s)[0])
    whole = _redact(path)
    if whole == path:
        return path
    segments = path.split(posixpath.sep)
    outs = [_redact(segment) for segment in segments]
    # Floor 1: segment-wise redaction must reproduce the whole-string result
    # byte for byte before any label is added. Anything the whole pass removed
    # that a single segment did not is a tail the caller must not see.
    if posixpath.sep.join(outs) != whole:
        return whole
    labelled = [
        (
            out
            if out == segment
            else f"{out}{_PATH_SEGMENT_DISCRIMINATOR_SEP}{_path_segment_label(segment)}"
        )
        for segment, out in zip(segments, outs)
    ]
    candidate = posixpath.sep.join(labelled)
    # Floor 2: the labelled result must itself be a fixed point of the redactor
    # (a shape that only matches in context, or one the labels complete).
    if _redact(candidate) != candidate:
        return whole
    return candidate

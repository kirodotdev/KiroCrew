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

The alternation and its pre-filter live in ``redaction_patterns`` and the
entropy machinery in ``redaction_entropy``, owner modules beside this one.
This module is the import path of every name they define, and keeps the batch
redactor with its pass plan and records, the pass-4 token-parameter grammar,
the redaction tags, the binary masker and the path passes. A read through this
module answers with the owner's object, and a write reaches every owner that
binds the name (:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import bisect
import contextvars
import functools
import hashlib
import hmac
import importlib as _importlib
import os
import posixpath
import re
import secrets
import sys as _sys
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.credential_patterns import AWS_KEY_ID
from kiro_crew.security.redaction_switch import bare_secret_pass_enabled, credential_pass_bypassed

from .redaction_entropy import (  # noqa: F401
    _B64_CHUNK_RE,
    _CS_DARWIN_USER_TEMP_DIR,
    _DARWIN_USER_DIR_PREFIX_RE,
    _DARWIN_USER_DIR_SUFFIX_RE,
    _DARWIN_USER_TEMP_DIR_RE,
    _DOCUMENT_LINK_RE,
    _IN_DIAGRAM_SCAN,
    _PLANTUML_RATIO_CAP,
    _PLANTUML_SOURCE_CAP,
    _RUN_PIECE_RES,
    _SECRET_KEY_LEN,
    _bare_secret_window_starts,
    _contains_bare_secret,
    _contains_fixed_credential,
    _decode_b64_chunk,
    _is_code_literal,
    _looks_like_secret_key,
    _plantuml_source,
    _text_contains_bare_secret,
)
from .redaction_patterns import _is_json_object_segment  # noqa: F401
from .redaction_patterns import _credential_matches, _might_contain_credential


def _plantuml_verdict(
    encoded: str, limit: int, token_param_prefixes: tuple[str, ...] | None = None
) -> tuple[bool, int]:
    """Judge one diagram; also return how much text was inflated for it.

    ``token_param_prefixes`` is the enclosing call's pass-4 snapshot, so the
    diagram's source is judged under the same prefix set as the text around it.
    """
    source = _plantuml_source(encoded, min(limit, _PLANTUML_RATIO_CAP * len(encoded)))
    if source is None:
        return False, min(limit, _PLANTUML_RATIO_CAP * len(encoded))
    # The link carries its source, so the source is judged by every credential
    # pass; anything they would mask leaves the link masked.
    previous = _IN_DIAGRAM_SCAN.get()
    _IN_DIAGRAM_SCAN.set(True)
    try:
        cleaned = redact_credentials(source, token_param_prefixes=token_param_prefixes)[0]
        return cleaned == source, len(source)
    finally:
        _IN_DIAGRAM_SCAN.set(previous)


def _document_link_spans(
    text: str, token_param_prefixes: tuple[str, ...] | None = None
) -> list[tuple[int, int]]:
    """Return the spans of the :data:`_DOCUMENT_LINK_RE` matches that hold no key."""
    spans = []
    budget = 0 if _IN_DIAGRAM_SCAN.get() else _PLANTUML_SOURCE_CAP
    for m in _DOCUMENT_LINK_RE.finditer(text):
        if m["diagram"]:
            kept, spent = _plantuml_verdict(m["diagram"], budget, token_param_prefixes)
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


# Standard replacement tag for a redacted credential. Shared between the batch
# redactor (`redact_credentials`) and the streaming fail-closed path
# (`StreamRedactor.feed`) so the on-the-wire marker is identical everywhere.
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


# ── `?token=` / `&token=` URL parameter values (pass 4) ──
# Keyed on the parameter NAME, not the value's shape, so an OPAQUE bearer value
# -- one that looks nothing like a JWT -- is redacted where every shape-based
# pattern in ``redaction_patterns`` sees ordinary text. A parameter name is a context: `?token=`
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
# no recovery -- the exact non-cosmetic false-positive surface ``redaction_patterns``
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
# Deliberately NOT a `_CREDENTIAL_PATTERNS` branch, for two reasons. A branch
# replaces its WHOLE span, which would swallow the `token=` anchor this pass
# exists to keep visible. And `_contains_fixed_credential` -- which gates
# request-BLOCKING decisions in `exfil.py` -- searches `_CREDENTIAL_PATTERNS`,
# so a branch would turn every `?token=` URL into a blocked request: a
# behaviour change the issue explicitly excludes. This pass redacts output
# only; the blocking surface is unchanged. The other credential-bearing
# parameter names (`access_token`, `id_token`, `api_key`, `code`) are excluded
# on the issue's own scoping ground -- each name wants its own false-positive
# analysis (`code=` especially collides with OAuth authorization codes AND
# ordinary prose) -- not because adding them HERE would change the blocking
# surface; a pass-4 name never feeds `_contains_fixed_credential`.
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
    rf"{_TOKEN_PARAM_SEP_RE}(?ai:{_TOKEN_PARAM_NAME_RE}){_TOKEN_PARAM_EQ_RE}(?!\$\{{)({_TOKEN_PARAM_VALUE_CLASS}+)"
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
_TOKEN_PARAM_PARTIAL_RE = re.compile(
    rf"(?:{_TOKEN_PARAM_SEP_RE}(?:(?ai:{_TOKEN_PARAM_NAME_PREFIX_RE})"
    rf"|(?ai:{_TOKEN_PARAM_NAME_RE})(?P<eq>{_TOKEN_PARAM_EQ_RE}){_TOKEN_PARAM_VALUE_CLASS}*)"
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


#: A well-formed companion prefix: ``https://host/path`` with no whitespace, query or
#: fragment. One without a path is dropped, since a bare host matches ``host.evil.example``.
_TOKEN_PARAM_EXEMPT_PREFIX_RE = re.compile(r"https://[^\s/?#&]+/[^\s?#&]*")

# A streaming window cannot prove the whole-message preceding-context guard.
_TOKEN_PARAM_EXEMPT_SUSPENDED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "token_param_exempt_suspended", default=False
)


def _token_param_exempt_prefixes() -> tuple[str, ...]:
    """Companion ``https://host/path`` prefixes whose ``?token=`` value pass 4 leaves alone.

    Read from ``CredentialPolicy.token_param_exempt_url_prefixes`` on the INSTALLED
    context only, degrading like ``exfil._exempt_exact_hosts``: no context, a
    pre-method adapter or an exception yields the empty tuple; malformed members are
    ignored. Both outcomes mean MORE redaction. No logging inside stdio MCP servers.
    """
    from kiro_crew.platform.context import installed_context

    ctx = installed_context()
    if ctx is None:
        return ()
    try:
        getter = getattr(ctx.credentials, "token_param_exempt_url_prefixes", None)
        if getter is None:
            return ()
        return _well_formed_token_param_prefixes(getter())
    except Exception:
        return ()


def _well_formed_token_param_prefixes(prefixes: Iterable[object]) -> tuple[str, ...]:
    """The members of *prefixes* pass 4 may honour, sorted; malformed ones drop.

    The one shape gate for the live read and for a caller's snapshot alike.
    """
    members = (p for p in prefixes if isinstance(p, str))
    return tuple(sorted(p for p in members if _TOKEN_PARAM_EXEMPT_PREFIX_RE.fullmatch(p)))


#: Single-character wrappers that may open an exempt URL. A ``(`` is accepted only
#: as the second half of a markdown ``](`` opener by ``_TokenParamExemptScan.boundary``.
_TOKEN_PARAM_EXEMPT_OPENERS = frozenset("<\"'`*")
_TOKEN_PARAM_EXEMPT_WHITESPACE = " \t\r\n"

#: An exempt value is ONE run of URL-unreserved, base64 or percent-encoded bytes plus
#: sentence/markdown-link closers. `_TOKEN_PARAM_VALUE_CLASS` admits `)`, `,`, `(`, `[`,
#: `:` and `?`, so one pass-4 match can swallow a second URL (`?token=k?token=SECRET`);
#: such a value is redacted whole, which is also its re-redaction fixed point.
_TOKEN_PARAM_EXEMPT_VALUE_RE = re.compile(r"[A-Za-z0-9._~%+/=-]+")
_TOKEN_PARAM_EXEMPT_TRAIL = frozenset(")].,;:!?*")
#: Scanned over RAW text, never decoded: any ``&`` (a character reference such as
#: ``&sol;`` or ``&#47;`` that a renderer decodes inside ``<...>`` or an attribute can
#: spell a destination) and any ``<`` (a tag or angle wrapper) are signals themselves.
_TOKEN_PARAM_DESTINATION_SIGNAL_RE = re.compile(r"//|\\|&|<|:(?=\S)")
_TOKEN_PARAM_SCHEME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-"
)


def _token_param_has_scheme_before(text: str, start: int, colon: int) -> bool:
    """Does the colon end a scheme-like run containing an ASCII letter?"""
    has_letter = False
    i = colon - 1
    while i >= start and text[i] in _TOKEN_PARAM_SCHEME_CHARS:
        has_letter = has_letter or "A" <= text[i] <= "Z" or "a" <= text[i] <= "z"
        i -= 1
    return has_letter


def _token_param_destination_signal(text: str, start: int, end: int) -> bool:
    """Find a raw destination signal in ``text[start:end]`` in linear work."""
    for signal in _TOKEN_PARAM_DESTINATION_SIGNAL_RE.finditer(text, start, end):
        if signal.group() != ":" or _token_param_has_scheme_before(text, start, signal.start()):
            return True
    return False


class _TokenParamExemptScan:
    """The two whole-message guards of the exemption, for ONE text's pass-4 matches.

    Both guards are stated against the text before a candidate -- the boundary
    guard against the whitespace-free run ending at the prefix, the destination
    guard against everything earlier with proven exempt URLs blanked -- and
    `_credential_redaction_plan` visits candidates left to right. So each guard
    keeps a cursor and reads every character once, which keeps pass 4 linear
    when untrusted text repeats an exempt URL thousands of times. Fixed-width
    signal matches plus disjoint colon-delimited scheme walks preserve that bound.
    The answers are those of the whole-prefix statements:

    * A destination signal found before one candidate is before every later one
      (a later exempt URL only blanks text after it, since a signal before it
      would have rejected it), so the first hit is sticky. Text before the last
      exempt URL's end is blanked or already scanned clear, so a scan starts there;
      a candidate's own ``<`` autolink opener is not a tag and is left out, and the
      segment ends where the URL would be blanked, so no signal straddles it.
    * The prefix has no whitespace, so the whitespace boundary of every candidate
      of one match is the last whitespace before its ``?``, which only moves right.
    * A whitespace-free run that already holds a matched prefix (exempt or not)
      puts that prefix's ``://`` inside every later candidate's ``left`` in the same
      run, so one rejected or accepted run is settled for the rest of it.
    """

    __slots__ = ("text", "cursor", "signal_seen", "sep", "ws_cursor", "last_ws", "left_floor")

    def __init__(self, text: str) -> None:
        self.text = text
        self.cursor = 0  # end of the last exempt URL: text before it is blanked or clear
        self.signal_seen = False
        self.sep = -1  # the `?` of the match whose candidates are being tried
        self.ws_cursor = 0
        self.last_ws = -1  # last whitespace before `sep`
        self.left_floor = 0  # a boundary below it has an earlier matched prefix in `left`

    def boundary(self, sep: int, start: int) -> bool:
        """Is *start* a standalone URL boundary rather than a URL nested in another?"""
        text = self.text
        if sep != self.sep:
            if self.sep >= 0:
                self.left_floor = max(self.left_floor, self.last_ws + 2)
            if sep > self.ws_cursor:
                last_ws = max(
                    text.rfind(c, self.ws_cursor, sep) for c in _TOKEN_PARAM_EXEMPT_WHITESPACE
                )
                if last_ws >= 0:
                    self.last_ws = last_ws
                self.ws_cursor = sep
            self.sep = sep
        boundary = self.last_ws + 1
        if boundary < self.left_floor:
            return False
        left = text[boundary:start]
        if "://" in left:
            return False
        markdown_opener = re.fullmatch(r"\[[^][]*\]\(", left) is not None
        return not left or left in _TOKEN_PARAM_EXEMPT_OPENERS or markdown_opener

    def preceding_context(self, start: int) -> bool:
        """Does the text before *start* lack evidence of an enclosing destination?"""
        if self.signal_seen:
            return False
        text = self.text
        # The one `<` that `boundary` accepted as the URL's own autolink opener is
        # not a tag; it is blanked with the URL once the URL is exempt.
        if start > 0 and text[start - 1] == "<":
            start -= 1
        # Unrelated earlier destinations may over-redact; that is the safe direction.
        if start > self.cursor and _token_param_destination_signal(text, self.cursor, start):
            self.signal_seen = True
            return False
        return True

    def accept(self, exempt_url: tuple[int, int]) -> None:
        """Record an exempt URL; its span is blanked for every later candidate."""
        self.cursor = exempt_url[1]


def _token_param_exempt(
    text: str,
    m: "re.Match[str]",
    prefixes: tuple[str, ...],
    scan: _TokenParamExemptScan,
) -> tuple[int, int] | None:
    """Return the exempt URL span for pass-4 match *m*, or ``None``."""
    sep = m.start()
    if not text.startswith("?token=", sep):
        return None
    run = _TOKEN_PARAM_EXEMPT_VALUE_RE.match(text, m.start(1))
    if run is None or not set(text[run.end() : m.end(1)]) <= _TOKEN_PARAM_EXEMPT_TRAIL:
        return None
    for prefix in prefixes:
        start = sep - len(prefix)
        if (
            start >= 0
            and text.startswith(prefix, start)
            and scan.boundary(sep, start)
            and scan.preceding_context(start)
        ):
            return start, run.end()
    return None


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


def redact_credentials(
    text: str,
    *,
    defer_token_param: bool = False,
    token_param_prefixes: tuple[str, ...] | None = None,
) -> tuple[str, list[str]]:
    """Redact raw credential patterns from text, including base64-encoded.

    Returns (cleaned_text, list_of_warnings).

    ``defer_token_param`` is for ONE caller: a streamed delta whose complete
    segment is redacted again, whole, before it is stored or sent as final. A
    delta cannot show pass 4 the companion prefix in front of a ``?token=``, so
    while ``CredentialPolicy.token_param_exempt_url_prefixes()`` is non-empty a
    ``token=`` value no earlier pass touched is left to that whole-segment run;
    a value an earlier pass already partly claimed is still redacted here, and
    every other pass still runs. With the empty default prefix set the output is
    byte-identical to a plain call.

    ``token_param_prefixes`` is a SNAPSHOT of that prefix set that stands in for
    pass 4's live read. A caller that keys a cache on the set passes the set it
    keyed on, so the output is computed under exactly that set. ``None``, the
    default, is the live read.
    """
    spans, warnings, _rules = _credential_redaction_plan(
        text, defer_token_param=defer_token_param, token_param_prefixes=token_param_prefixes
    )
    if not spans:
        return text, warnings
    return _splice(text, spans), warnings


#: Label forms of the key-value AWS branches: the key name, its separator and
#: an optional opening quote. The redactor replaces the WHOLE match, label
#: included, so a record keeps the label to let the reader see which field
#: the removed value belonged to. A label is a fixed key name, never secret.
_AWS_LABEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?:SecretAccessKey|aws_secret_access_key)[\"']?\s*[:=]\s*[\"']?"),
        "aws_secret_access_key",
    ),
    (re.compile(r"(?:SessionToken|aws_session_token)[\"']?\s*[:=]\s*[\"']?"), "aws_session_token"),
    (re.compile(r"(?:AccessKeyId|aws_access_key_id)[\"']?\s*[:=]\s*[\"']?"), "aws_access_key_id"),
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


def redact_credentials_with_records(
    text: str, *, defer_token_param: bool = False
) -> tuple[str, list[str], list[CredentialMatch]]:
    """:func:`redact_credentials`, plus one :class:`CredentialMatch` per tag written.

    The cleaned text and warnings are byte-identical to ``redact_credentials``
    with the same ``defer_token_param``; both share one plan, so the records
    cannot describe a redaction the text does not contain.
    """
    spans, warnings, rules = _credential_redaction_plan(text, defer_token_param=defer_token_param)
    if not spans:
        return text, warnings, []
    matches: list[CredentialMatch] = []
    ordinal = 0
    cursor = 0
    for start, end, tag in spans:
        ordinal += sum(text.count(t, cursor, start) for t in CREDENTIAL_REDACTION_TAGS)
        rule, label = rules.get(start, ("credential_pattern", ""))
        matches.append(CredentialMatch(ordinal, rule, label, text[start:end][len(label) :]))
        ordinal += 1
        cursor = end
    return _splice(text, spans), warnings, matches


def _credential_redaction_plan(
    text: str,
    *,
    defer_token_param: bool = False,
    token_param_prefixes: tuple[str, ...] | None = None,
) -> tuple[list[_RedactionSpan], list[str], dict[int, tuple[str, str]]]:
    """Every span :func:`redact_credentials` rewrites, with its warnings and rules.

    Returns ``(spans, warnings, rules)``: ``spans`` sorted and disjoint against
    the input, ``rules`` mapping each span's start to its ``(rule_id, label)``.
    ``defer_token_param`` and ``token_param_prefixes`` are
    :func:`redact_credentials`'s keywords of those names.

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

    Inside a ``redaction_switch.code_text()`` scope pass 3 alone stands down.
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
            warnings.append(f"Redacted credential pattern ({len(m.group())} chars)")
            taken.append((m.start(), m.end(), _REDACTED_CREDENTIAL_TAG))
            rules[m.start()] = _pass1_rule(m.group())

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
    #
    # A `redaction_switch.code_text()` scope leaves alone a data URI payload and an ordered
    # alphabet (see `_is_code_literal`); every other run is judged as outside the scope.
    bare_runs = (
        b64_matches
        if bare_secret_pass_enabled()
        else [
            m for m in b64_matches if not _is_code_literal(text, m.start(), m.group().rstrip("="))
        ]
    )
    withheld = _darwin_user_dir_ids(text)
    pass3: list[_RedactionSpan] = []
    link_spans: list[tuple[int, int]] | None = None
    for m in bare_runs:
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
            link_spans = _document_link_spans(text, token_param_prefixes)
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
    # skipped, not re-redacted: several surfaces run the redactor twice (the
    # streaming path re-redacts the persisted copy; `redact_path_segments`
    # requires its candidate to be a fixed point), and the value class stops at
    # a tag's interior space. Matching a canonical tag again would mangle
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
    # COMPANION URL-PREFIX SEAM: a value this pass would claim WHOLE (no earlier
    # pass found anything in it) is left alone when `_token_param_exempt` ties it to
    # a `CredentialPolicy.token_param_exempt_url_prefixes()` member, e.g. an
    # approval-workflow link the user must click. The prefixes are read lazily, on
    # the first such match, so text with no `token=` never touches the context.
    #
    # DEFERRED DELTA: a streamed delta can carry the `?token=` without the prefix
    # that precedes it, so the exemption cannot be judged here; with a prefix set
    # installed, a value this pass would claim WHOLE is left to the caller's
    # whole-segment run (see `redact_credentials`). Only such values are eligible
    # for the exemption, so only they are deferred: a value an earlier pass has
    # already partly claimed is redacted now, gaps and all, exactly as without the
    # keyword -- the whole-segment run skips a value that begins with a fixed tag,
    # so an uncovered tail left here would never be revisited. The stream
    # suspension wins when both are set: it pins the prefixes empty, and an empty
    # set defers nothing.
    #
    # SNAPSHOT: `token_param_prefixes` replaces the lazy live read with the set a
    # caller keyed its cache on. It passes the live read's own shape gate, so it
    # cannot exempt a URL the live read could not, and the suspension still wins.
    pass4: list[_RedactionSpan] = []
    exempt_prefixes: tuple[str, ...] | None = None
    if _TOKEN_PARAM_EXEMPT_SUSPENDED.get():
        exempt_prefixes = ()
    elif token_param_prefixes is not None:
        exempt_prefixes = _well_formed_token_param_prefixes(token_param_prefixes)
    exempt_scan = _TokenParamExemptScan(text)
    for m in _TOKEN_PARAM_RE.finditer(text):
        value_start, value_end = m.start(1), m.end(1)
        if any(text.startswith(tag, value_start) for tag in CREDENTIAL_REDACTION_TAGS):
            continue
        gaps = _uncovered(value_start, value_end, taken)
        if not gaps:
            continue
        if gaps == [(value_start, value_end)]:
            if exempt_prefixes is None:
                exempt_prefixes = _token_param_exempt_prefixes()
            if exempt_prefixes:
                if defer_token_param:
                    continue
                exempt_url = _token_param_exempt(text, m, exempt_prefixes, exempt_scan)
                if exempt_url is not None:
                    exempt_scan.accept(exempt_url)
                    continue
        for start, end in gaps:
            pass4.append((start, end, _REDACTED_CREDENTIAL_TAG))
            rules[start] = ("token_parameter", "")
        warnings.append(f"Redacted token parameter value ({value_end - value_start} chars)")

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


# This module is the import path of every name the output redactors offer. The batch
# redactor, its pass plan and the path passes above stay here; the alternation and the
# entropy machinery live in the owner modules below, and a write through this module (a
# patch) reaches every module holding the name (:mod:`kiro_crew._facade_mirror`). The
# header also binds the owner names ``text_redaction`` and ``display_safety`` read here.
# Owners never import this module. New work goes to the owner of its responsibility:
#   ``redaction_patterns`` -- the credential alternation and its pre-filter, one unit
#   ``redaction_entropy`` -- the bare-secret shape gates, the base64 decode and the
#   document-link grammar
from kiro_crew import _facade_mirror  # noqa: E402

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.security.redaction_patterns",
    "kiro_crew.security.redaction_entropy",
)  # the loop below imports them in this order
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace, and the
#: ``TYPE_CHECKING`` flag the glue imports.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.security.redaction_entropy import (  # noqa: F401
        _ALPHABET_RUN_MAX,
        _ALPHABET_STEPS,
        _BARE_SECRET_RUN_RE,
        _DATA_URI_BEFORE_RE,
        _DATA_URI_PAYLOAD_MIN,
        _DATA_URI_REACH,
        _DOCUMENT_ID,
        _ENTROPY_TERMS_KEY_LEN,
        _GOOGLE_USER,
        _HEX_ONLY_RE,
        _HOST_ID_EXEMPT_OVERLAP,
        _PLANTUML_ALPHABET,
        _PLANTUML_DIAGRAM,
        _PRINTABLE_BYTES,
        _ROUTED_COMMIT_PIECE_MIN,
        _ROUTED_COMMIT_RE,
        _SECRET_ENTROPY_MIN,
        _SECRET_MAX_LOWER_RUN,
        _SECRET_MAX_SLASHES,
        _SECRET_MAX_VOWEL_RATIO,
        _SECRET_PRINTABLE_DECODE_RATIO,
        _VOWELS,
        _crosses_into_a_routed_commit,
        _decode_b64_safe,
        _decodes_to_printable_text,
        _has_all_three_char_classes,
        _lowercase_run_exceeds,
        _shannon_entropy,
        _steps_up,
        _vowel_ratio,
    )
    from kiro_crew.security.redaction_patterns import (  # noqa: F401
        _CREDENTIAL_PATTERNS,
        _CREDENTIAL_PATTERNS_NO_PEM_BLOCK,
        _CREDENTIAL_PATTERNS_SANS_JWT,
        _CREDENTIAL_PREFILTER_AUTHORIZATION_RE,
        _CREDENTIAL_PREFILTER_AUTHORIZATION_UPPER_RE,
        _CREDENTIAL_PREFILTER_DISCORD_RE,
        _CREDENTIAL_PREFILTER_GH_RE,
        _CREDENTIAL_PREFILTER_LITERALS,
        _CREDENTIAL_PREFILTER_TELEGRAM_RE,
        _CREDENTIAL_PREFILTER_URI_RE,
        _CREDENTIAL_STARTS_SANS_JWT,
        _JWT_SEGMENT_RUN_RE,
        _PREFILTER_MIN_LEN,
        _TELEGRAM_AT_RE,
        _TELEGRAM_BRANCH,
        _contains_credential_pattern,
        _credential_search,
        _jwt_branch_matches_at,
        _next_jwt_branch_start,
        _sans_jwt_start,
        _segment_run_end,
        get_credential_patterns,
    )
if TYPE_CHECKING:  # project names it serves from outside its owners, from their homes
    import kiro_crew.security.pem_scan as pem_scan  # noqa: F401
    from kiro_crew.credential_patterns import (  # noqa: F401
        JWT_MULTI_SEGMENT,
    )

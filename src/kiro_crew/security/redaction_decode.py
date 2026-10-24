"""Encoded credentials: a base64 run that decodes to a secret.

Pass 2 of the batch redactor. A run of the base64 alphabet (standard or
URL-safe, on one line or wrapped over several) is decoded, and the decoded text
is checked with the same rules the plain text is; when it holds a secret, the
whole encoded run is replaced.

:mod:`kiro_crew.security.redaction` is the import path of every name defined
here.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterator

from kiro_crew.credential_patterns import URL_SIGNING_VALUE

from .redaction_labels import (
    BLOCKING_RULE_IDS,
    QUOTED_HOLD_REACH,
    has_context_hint,
    label_spans,
    paired_secret_spans,
    uri_password_spans,
)
from .redaction_normalise import changed_windows, normalised_views, window_edges
from .redaction_patterns import (
    _PREFILTER_MIN_LEN,
    _contains_credential_pattern,
    _might_contain_credential,
)
from .secret_catalog import catalog_findings, has_catalog_keyword

# Base64 alphabet: at least 40 chars of [A-Za-z0-9+/] ending with optional =
_B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

#: A whole standard base64 run of 24 to 39 characters: a short labelled secret
#: in compact JSON (``{"password":"abcdefgh"}`` encodes to 32). Read only by
#: the decoded-credential scan (:func:`_decoded_runs`), whose decoded-text test
#: needs a label or a vendor keyword, so ordinary short tokens stay.
_B64_SHORT_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{24,39}={0,2}(?![A-Za-z0-9+/=])")

#: The URL-safe alphabet (``-`` and ``_`` for ``+`` and ``/``). Only a run that
#: carries one of the two URL-safe characters is a URL-safe candidate; any other
#: run is already a standard chunk.
_B64URL_CHUNK_RE = re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*[_-])[A-Za-z0-9_-]{40,}={0,2}")

#: Base64 wrapped over lines, as PEM bodies, MIME parts and ``base64`` (default
#: 76 columns) write it: one or more full lines, each ended by a newline or a
#: literal ``\n``, then a last line.
_B64_WRAPPED_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{16,}(?:\r?\n|\\n))+[A-Za-z0-9+/]{4,}={0,2}"
)
_LINE_BREAK_RE = re.compile(r"\r?\n|\\n")

# A presigned/signed link's signing value. A base64-encoded SigV2 link decodes to
# a URL whose ``&Signature=<base64>`` is not a pass-1 shape, so the decoded text is
# also checked for it.
_URL_SIGNING_RE = re.compile(URL_SIGNING_VALUE)


#: Printable ASCII and the three blanks: what a decoded TEXT is made of.
_TEXT_CHARS = {c: None for c in range(32, 127)} | {9: None, 10: None, 13: None}

#: The share of control characters above which a decode is binary (an image, a
#: compressed blob): random bytes decode to about a third of control bytes.
_BINARY_SHARE = 0.05


#: A run of printable text inside a binary decode, long enough to be a label
#: and its value.
_TEXT_RUN_RE = re.compile(r"[ -~\t]{16,}")


def _is_binary(decoded: str) -> bool:
    """Whether *decoded* is binary data rather than text: more than
    :data:`_BINARY_SHARE` of it is control characters. Printable Unicode (a
    password in another script) is text like printable ASCII."""
    rest = decoded.translate(_TEXT_CHARS)
    if len(rest) <= _BINARY_SHARE * len(decoded):
        return False
    controls = sum(1 for c in rest if not c.isprintable())
    return controls > _BINARY_SHARE * len(decoded)


def _holds_secret(decoded: str) -> bool:
    """Whether decoded text, or its normalised view (escapes and percent-codes
    read, never base64 again), holds anything the plain-text rules redact.

    A binary decode (``base64 logo.png``) is read for the fixed patterns only:
    its views and the context rules, built over hundreds of kilobytes of random
    bytes, would hold the caller for seconds and can only match by chance."""
    if _is_binary(decoded):
        # UTF-16 text (PowerShell's ``-EncodedCommand``) decodes with a NUL
        # beside every ASCII character: without them it is text.
        wide = decoded.replace("\x00", "")
        if len(wide) < len(decoded) and not _is_binary(wide):
            return _holds_secret(wide)
        if _holds_plain_secret(decoded, context=False):
            return True
        # A binary container's text chunks (a PNG ``tEXt``, EXIF, XMP) are read
        # whole, the binary between them left out: random bytes seldom hold a
        # printable run this long, so the context rules see only real text.
        regions = "\n".join(_TEXT_RUN_RE.findall(decoded))
        return bool(regions) and _holds_secret(regions)
    if _holds_plain_secret(decoded):
        return True
    return any(_holds_plain_secret(view.text) for view in normalised_views(decoded))


def _holds_plain_secret(decoded: str, *, context: bool = True) -> bool:
    """Whether *decoded*, as written, holds anything the plain-text rules redact;
    the context rules only when *context*."""
    if _URL_SIGNING_RE.search(decoded) is not None:
        return True
    # Gate the alternation behind the cheap superset pre-filter, as pass 1 does;
    # below `_PREFILTER_MIN_LEN` the alternation is the cheaper of the two.
    if len(decoded) < _PREFILTER_MIN_LEN or _might_contain_credential(decoded):
        if _contains_credential_pattern(decoded):
            return True
    if not context:
        return False
    # The context rules all start at a keyword: a label, a key ID prefix, a URI
    # scheme or a vendor-catalog keyword. Decoded text with none of them (the
    # random bytes most base64 runs in a large file decode to) holds no match.
    if not has_context_hint(decoded) and not has_catalog_keyword(decoded):
        return False
    return bool(
        label_spans(decoded)
        or paired_secret_spans(decoded)
        or uri_password_spans(decoded)
        or catalog_findings(decoded)
    )


def _decode(chunk: str, *, urlsafe: bool = False) -> str:
    """*chunk* decoded as UTF-8 text (invalid bytes dropped), or ``""``."""
    # NO LENGTH SHORT-CIRCUIT: base64 padding leniency differs across Python
    # versions, so whether a chunk decodes is left to the decoder itself.
    try:
        if urlsafe:
            raw = base64.urlsafe_b64decode(chunk + "=" * (-len(chunk.rstrip("=")) % 4))
        else:
            # Padding is often stripped (a URL ``state``, a JWT-style field):
            # restore it, so an unpadded run decodes like a padded one.
            raw = base64.b64decode(chunk + "=" * (-len(chunk) % 4), validate=True)
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="ignore")


def _decode_b64_chunk(chunk: str) -> str:
    """Decode ONE standard `_B64_CHUNK_RE` match; return it when it holds a secret, else ''."""
    decoded = _decode(chunk)
    return decoded if decoded and _holds_secret(decoded) else ""


def _decode_b64_safe(text: str) -> str:
    """The first standard base64 chunk in *text* that decodes to a secret, decoded, or ''."""
    for m in _B64_CHUNK_RE.finditer(text):
        decoded = _decode_b64_chunk(m.group())
        if decoded:
            return decoded
    return ""


def _contains_fixed_credential(text: str) -> bool:
    """Return True for a credential that blocks a request.

    Gates request-BLOCKING decisions in the exfiltration checks and the static
    preview: the fixed patterns and the context rules of the Secret tier (an AWS
    or ``Authorization`` label, a key pair, a catalog finding, an encoded
    credential), on the text and on its normalised view. A generic labelled
    value or a signing value redacts output but does not block.
    """
    return _contains_credential_pattern(text) or blocking_context(text) or _view_blocks(text)


def blocking_context(text: str) -> bool:
    """The context rules of :func:`_contains_fixed_credential`, without pass 1."""
    # Only a Secret-tier label blocks. A generic labelled value and a signing
    # value (Bearer link tier: it masks, as ``?token=`` does) are redacted in
    # output and are not, alone, grounds to refuse a request.
    return _plain_context_blocks(text) or _encoded_blocks(text)


def _plain_context_blocks(text: str) -> bool:
    """The blocking context rules on *text* as written, without decoding."""
    if any(span.rule in BLOCKING_RULE_IDS for span in label_spans(text)):
        return True
    return bool(paired_secret_spans(text) or catalog_findings(text))


def view_texts(text: str) -> Iterator[str]:
    """Every decoded layer of every window around a character normalising
    changes: the texts a blocking check reads besides *text* itself."""
    covered = 0  # a widened window already read every window ending before this
    for lo, hi in changed_windows(text, QUOTED_HOLD_REACH):
        if hi <= covered:
            continue
        lo, hi = window_edges(text, max(lo, covered - QUOTED_HOLD_REACH), hi)
        covered = hi
        for view in normalised_views(text[lo:hi]):
            yield view.text


def _view_blocks(text: str) -> bool:
    """Whether the normalised view, around the characters it changes, blocks."""
    return any(
        _contains_credential_pattern(view) or blocking_context(view) for view in view_texts(text)
    )


def _decoded_runs(text: str) -> Iterator[tuple[int, int, str]]:
    """``(start, end, decoded)`` of every base64 run in *text*: standard,
    URL-safe and wrapped over lines. The ONE scanner both redaction and blocking
    read, so a run one of them decodes the other decodes too."""
    for m in _B64_CHUNK_RE.finditer(text):
        yield m.start(), m.end(), _decode(m.group())
    for m in _B64_SHORT_RE.finditer(text):
        yield m.start(), m.end(), _decode(m.group())
    for m in _B64URL_CHUNK_RE.finditer(text):
        yield m.start(), m.end(), _decode(m.group(), urlsafe=True)
    for m in _B64_WRAPPED_RE.finditer(text):
        yield m.start(), m.end(), _decode(_LINE_BREAK_RE.sub("", m.group()))


def encoded_credential_spans(text: str) -> Iterator[tuple[int, int]]:
    """Spans of every base64 run in *text* that decodes to a secret.

    Standard runs first, then URL-safe and wrapped runs; spans may overlap and
    the caller merges them.
    """
    for start, end, decoded in _decoded_runs(text):
        if decoded and _holds_secret(decoded):
            yield start, end


def _encoded_blocks(text: str) -> bool:
    """Whether any base64 run (:func:`_decoded_runs`) decodes to a credential
    that blocks: a fixed pattern or a blocking context rule, on the decoded text
    and its normalised view (never base64 again)."""
    for _start, _end, decoded in _decoded_runs(text):
        if not decoded:
            continue
        for candidate in (decoded, *(view.text for view in normalised_views(decoded))):
            if _contains_credential_pattern(candidate):
                return True
            # The context rules all start at a keyword: skip the decoded text
            # (most often random bytes) that holds none.
            if (
                has_context_hint(candidate) or has_catalog_keyword(candidate)
            ) and _plain_context_blocks(candidate):
                return True
    return False

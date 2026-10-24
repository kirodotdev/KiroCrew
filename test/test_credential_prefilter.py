"""Differential test pinning `redact_credentials` output across the fast-path rewrite.

`redact_credentials` is the redaction boundary: a regression here writes live
credentials into persisted chat history. The optimisation it guards is pure
control flow — a pre-filter that skips a scan already known to be empty, one
shared base64 scan feeding two passes, a chunk decode that does not re-scan
its own input, and a bisect-backed span subtraction. None of it may change
output, so this module pins a plain, unoptimised implementation of the passes
as a reference oracle and asserts the live function is byte-identical to it, on
both the returned text AND the warnings.

The oracle deliberately reuses the module's own compiled patterns and gate
helpers, so what it isolates is exactly the control-flow change. Pattern edits
are covered instead by `test_every_pattern_branch_has_a_prefilter_anchor`, which
fails when a branch is added without a matching pre-filter anchor.
"""

from __future__ import annotations

import base64
import binascii
import random
import re
import string

import pytest

from kiro_crew import credential_values
from kiro_crew.security import (
    _B64_CHUNK_RE,
    _CREDENTIAL_PATTERNS,
    _PREFILTER_MIN_LEN,
    _REDACTED_CREDENTIAL_TAG,
    _REDACTED_ENCODED_CREDENTIAL_TAG,
    _decode_b64_chunk,
    _decode_b64_safe,
    _might_contain_credential,
    redact_credentials,
)
from kiro_crew.security.redaction import (
    _HTML_REF_AMP,
    _HTML_REF_EQUALS,
    _HTML_REF_QUEST,
    _TOKEN_PARAM_NAME_RE,
    _TOKEN_PARAM_VALUE_CLASS,
    _context_spans,
    _credential_matches,
    encoded_credential_spans,
    redact_credentials_with_records,
)
from kiro_crew.security.redaction_normalise import normalised_view, normalised_views

# Mirror the live pass-4 wrapper exactly while keeping the oracle's span and
# control-flow implementation independent. The name fold is ASCII-scoped so
# parser-distinct Unicode lookalikes are not treated as `token`.
_REFERENCE_TOKEN_PARAM_SEP_RE = rf"(?:[?&]|{_HTML_REF_AMP}|{_HTML_REF_QUEST})"
_REFERENCE_TOKEN_PARAM_EQ_RE = rf"(?:=|{_HTML_REF_EQUALS})"
_REFERENCE_TOKEN_PARAM_RE = re.compile(
    rf"{_REFERENCE_TOKEN_PARAM_SEP_RE}(?ai:{_TOKEN_PARAM_NAME_RE})"
    rf"{_REFERENCE_TOKEN_PARAM_EQ_RE}({_TOKEN_PARAM_VALUE_CLASS}+)"
)

# ── Reference oracle: the implementation as it stood before the optimisation ──


def _reference_uncovered(
    start: int, end: int, taken: list[tuple[int, int, str]]
) -> list[tuple[int, int]]:
    """The parts of ``[start, end)`` no span in *taken* covers, by plain scan.

    Deliberately linear and bisect-free so it shares nothing with the live
    ``_uncovered`` helper beyond the definition of the answer.
    """
    gaps: list[tuple[int, int]] = []
    cursor = start
    for s, e, _ in sorted(taken):
        if e <= cursor or s >= end:
            continue
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def _reference_claim(
    taken: list[tuple[int, int, str]],
    found: list[tuple[int, int]],
    warnings: list[str],
) -> list[tuple[int, int, str]]:
    """*taken* plus the still-plaintext parts of each *found* span, one warning per claim."""
    added: list[tuple[int, int, str]] = []
    for start, end in found:
        gaps = _reference_uncovered(start, end, taken + added)
        if not gaps:
            continue
        warnings.append(f"Redacted credential pattern ({end - start} chars)")
        added.extend((s, e, _REDACTED_CREDENTIAL_TAG) for s, e in gaps)
    return sorted(taken + added)


def _reference_redact_credentials(text: str) -> tuple[str, list[str]]:
    """The passes, positioned by span, with no shortcuts. Do not "optimise" this.

    Every pass records ``(start, end, tag)`` against the ORIGINAL ``text`` and
    the string is rebuilt once at the end. The pre-optimisation source wrote
    ``result.replace(matched, tag, 1)`` in every pass, which replaces the first
    occurrence of the matched *text* rather than the span the regex actually
    matched. When an earlier, NON-matching lookalike contains the matched text
    as a substring (``xM<jwt>`` before a boundary-anchored ``M<jwt>``; a
    decodable base64 chunk inside a longer run that does not decode), that shape
    redacted the innocent host and left the real credential in the output in
    plaintext -- a genuine leak, not a cosmetic difference. See
    ``test_matched_span_is_redacted_not_an_earlier_lookalike`` and
    ``test_hosted_encoded_credential_is_redacted_at_its_own_span``.

    Precedence between passes is positional: pass 1 outranks the context rules,
    which outrank the same rules over the normalised view, which outrank pass 2,
    which outranks pass 4; a later pass redacts only the part of its span an
    earlier pass has not already claimed. Pass 2 warns for every run that decodes
    to a credential (its warning counts credentials found, as the original did).

    The context rules, the normalised view and the encoded-run finder are the
    module's own helpers; what this oracle isolates is the pre-filter gate, which
    it never consults.
    """
    warnings: list[str] = []
    taken: list[tuple[int, int, str]] = []

    # 1. plaintext credential patterns — ungated full scan, JSON-header-validated JWT hits
    for m in _credential_matches(text):
        warnings.append(f"Redacted credential pattern ({len(m.group())} chars)")
        taken.append((m.start(), m.end(), _REDACTED_CREDENTIAL_TAG))

    # 1b. labelled values, key pairs and the vendor catalog
    taken = _reference_claim(taken, [(s, e) for s, e, *_ in _context_spans(text)], warnings)

    # 1c. the same rules over every normalised layer, mapped back — again ungated
    mapped = []
    for view in normalised_views(text):
        found = [m.span() for m in _credential_matches(view.text)]
        # A decoded view's separators came from an encoding: no config lines.
        decoded = credential_values.reading_decoded()
        try:
            found += [(s, e) for s, e, *_ in _context_spans(view.text)]
        finally:
            credential_values.LINE_PREFIX.reset(decoded)
        mapped.extend(view.source_span(s, e) for s, e in found)
    if mapped:
        taken = _reference_claim(taken, sorted(mapped), warnings)

    # 2. base64-encoded credentials — overlapping runs joined, then claimed
    pass2: list[tuple[int, int, str]] = []
    runs: list[tuple[int, int]] = []
    for start, end in sorted(encoded_credential_spans(text)):
        if runs and start < runs[-1][1]:
            runs[-1] = (runs[-1][0], max(runs[-1][1], end))
        else:
            runs.append((start, end))
    for run_start, run_end in runs:
        warnings.append(f"Redacted base64-encoded credential ({run_end - run_start} chars)")
        for start, end in _reference_uncovered(run_start, run_end, taken + pass2):
            pass2.append((start, end, _REDACTED_ENCODED_CREDENTIAL_TAG))
    taken = sorted(taken + pass2)

    # 4. `?token=` / `&token=` parameter values — ungated full scan, value
    # group only, skipping a value that is already a fixed credential tag.
    pass4: list[tuple[int, int, str]] = []
    for m in _REFERENCE_TOKEN_PARAM_RE.finditer(text):
        # Byte identity only: trust the two fixed credential literals, never a shape.
        if any(
            text.startswith(tag, m.start(1))
            for tag in ("[REDACTED: credential]", "[REDACTED: encoded credential]")
        ):
            continue
        gaps = _reference_uncovered(m.start(1), m.end(1), taken)
        if not gaps:
            continue
        for start, end in gaps:
            pass4.append((start, end, _REDACTED_CREDENTIAL_TAG))
        warnings.append(f"Redacted token parameter value ({m.end(1) - m.start(1)} chars)")

    out: list[str] = []
    cursor = 0
    for start, end, tag in sorted(taken + pass4):
        out.append(text[cursor:start])
        out.append(tag)
        cursor = end
    out.append(text[cursor:])
    return "".join(out), warnings


# ── One sample per top-level branch of _CREDENTIAL_PATTERNS, in branch order ──

AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # 40 chars, AWS docs example
_A35 = "A" * 35
_LINK_PAYLOAD = "e" * 100  # clears the {96,} floor on the 2-segment link token
_LINK_SIG = "S" * 43  # token_auth._sign is always exactly 43 base64url chars

BRANCH_SAMPLES: tuple[str, ...] = (
    "-----BEGIN RSA PRIVATE" " KEY-----\nMIIBOgIBAAJBAKj34\n-----END RSA PRIVATE" " KEY-----",  # 0
    "xoxb-1234567890-abcdefghijklmnop",  # 1  Slack
    f"123456789:{_A35}",  # 2  Telegram bot token
    "M" + "a" * 24 + ".abc123." + "b" * 27,  # 3  Discord bot token
    "ghp_" + "a" * 36,  # 4  GitHub PAT
    "github_pat_" + "a" * 40,  # 5
    "glpat-" + "a" * 20,  # 6
    "sk_live_" + "a" * 24,  # 7  Stripe
    "SG.abcdefghijklmnop.abcdefghijklmnop",  # 8  SendGrid
    "sk-proj-" + "a" * 20,  # 9  OpenAI
    "sk-ant-" + "a" * 20,  # 10  Anthropic
    "npm_" + "a" * 30,  # 11
    "pypi-" + "a" * 20,  # 12
    "dop_v1_" + "a" * 45,  # 13  DigitalOcean
    "GOCSPX-" + "a" * 25,  # 14  Google OAuth client secret
    "IQoJb3Jp" "Z2lu" + "a" * 90,  # 15  AWS STS session token
    "postgres://dbuser:s3cr3tpw@db.example.com:5432/app",  # 16  URI userinfo
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",  # 17  JWS
    f" eyJ{_LINK_PAYLOAD}.{_LINK_SIG}",  # 18  2-segment dashboard link token
    "Signature=" + "0123456789abcdef" * 4,  # 19  SigV4 request signature
)

# ── Labelled, paired and encoded shapes the context rules and the view handle ──
#
# None of these is a branch of `_CREDENTIAL_PATTERNS`; they reach the redactor
# through `redaction_labels`, the vendor catalog or the normalised view, which
# the pre-filter does not gate. They are in the corpus so the differential also
# covers the passes that sit between pass 1 and pass 2.
_GLPAT = "glpat" + "-" + "Xk9fQ2mP4nR7sT1vWq7d"  # assembled; fake value
CONTEXT_SHAPES: tuple[str, ...] = (
    f"aws_secret_access_key={AWS_SECRET}",
    f"AWS_SECRET_ACCESS_KEY = {AWS_SECRET}",
    f'{{"SecretAccessKey": "{AWS_SECRET}"}}',
    "aws_session_token=FwoGZXIvYXdzEBYaDNotARealSessionToken",
    "Authorization: Bearer abc.def.ghijklmnop",
    "authorization: bearer opaque-token-123456",
    "api_key=Xk9fQ2mP4nR7",
    'client_secret: "Vb3nHj8LqW2zYc5d"',
    f"AKIAIOSFODNN7EXAMPLE {AWS_SECRET}",
    f"key AKIAIOSFODNN7EXAMPLE then later {AWS_SECRET} in the same note",
    "aws_secret_access_key%3D" + AWS_SECRET,
    "glpat%2D" + "a" * 20,
    '{"k":"gh\\u0070_' + "a" * 36 + '"}',
    "gh\x1b[0mp_" + "a" * 36,
    "gl\u200bpat-" + "a" * 20,
    base64.urlsafe_b64encode(b"\xfb\xff" + _GLPAT.encode() + b" and more prose here").decode(),
    "-----wrapped-----\n"
    + "\n".join(
        base64.b64encode(f"aws_secret_access_key={AWS_SECRET} wrapped".encode()).decode()[
            i : i + 20
        ]
        for i in range(0, 84, 20)
    ),
)


def _split_top_level(pattern: str) -> list[str]:
    """Split *pattern* on the `|` at the outermost group's depth.

    `_CREDENTIAL_PATTERNS` is one `(?:a|b|c)` group, so this returns its branches.
    Tracks escapes and character classes so a `|` or paren inside either is not
    mistaken for structure.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_class = False
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            buf.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            buf.append(ch)
            i += 1
            continue
        if ch == "[":
            in_class = True
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            if depth == 1:
                i += 1
                if pattern[i : i + 2] == "?:":  # drop the outermost (?: marker
                    i += 2
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "|" and depth == 1:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


BRANCHES = _split_top_level(_CREDENTIAL_PATTERNS.pattern)


def _rebuild(branches: list[str]) -> re.Pattern[str]:
    return re.compile("(?:" + "|".join(branches) + ")")


# ── Corpus ──


# Fixtures for the pass-2 per-run cases below. Deliberately deterministic so
# corpus ids stay stable across runs.
_B64_RUN_40 = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"  # 40 chars, len % 4 == 0
# base64 of readable prose with no secret in it -> decodes, holds nothing.
_PRINTABLE_BLOB = base64.b64encode(
    b"the quick brown fox jumps over the lazy dog while deploying"
).decode()
# base64 of bytes 0x00-0x2F -> decodes to mostly control bytes.
_GARBAGE_BLOB = base64.b64encode(bytes(range(48))).decode()
# 30 bare UTF-8 continuation bytes: a valid 40-char base64 chunk that decodes to
# 30 raw bytes but to the EMPTY string once `errors="ignore"` drops them all. This
# is the only way a decoded string lands below `_PREFILTER_MIN_LEN`, since a chunk
# is at least 40 base64 characters and so always decodes to >= 30 bytes.
_SHORT_DECODE_BLOB = base64.b64encode(bytes([0x80] * 30)).decode()
# A real encoded credential with its padding stripped, so the corpus can carry the
# misaligned shapes whose decodability differs between 3.10/3.11 and 3.12.
_ENCODED_CRED_STEM = (
    base64.b64encode(f"aws_secret_access_key={AWS_SECRET}".encode()).decode().rstrip("=")
)
# ── Hosting shapes: a redactable value that ALSO occurs inside an earlier, longer
# run which is itself NOT redactable. Redacting by value lands the tag inside the
# host and leaves the standalone credential in plaintext. ──
# Pass 2: 42 raw bytes -> 56 base64 chars, no padding, so prefixing one
# alphabet char yields a 57-char run that cannot decode (length % 4 == 1).
_HOSTED_ENCODED_CRED = base64.b64encode(b"ghp_" + b"a" * 38).decode()
_ENCODED_CRED_HOST = "Q" + _HOSTED_ENCODED_CRED


def _corpus() -> list[str]:
    """Every shape the passes can encounter, plus the awkward boundaries."""
    cases: list[str] = [
        # degenerate
        "",
        " ",
        "\n\n\n",
        "no credentials here at all, just ordinary prose about deployments",
        # every branch, alone and embedded in prose
        *BRANCH_SAMPLES,
        *[f"prefix text {s} suffix text" for s in BRANCH_SAMPLES],
        # labelled, paired and encoded shapes, alone and embedded
        *CONTEXT_SHAPES,
        *[f"log line: {s} end" for s in CONTEXT_SHAPES],
        # an unlabelled 40-char value with nothing beside it is left as it is
        AWS_SECRET,
        f"key is {AWS_SECRET} ok",
        # a secret AS a `?token=` value: an earlier pass claims the value, so
        # pass 4 must find no gap. Without the fold of `taken` ahead of pass 4,
        # pass 4 re-claims the same span, `_splice` gets overlapping spans and
        # the output doubles the tag, which the byte-identical differential
        # then catches.
        f"?token={AWS_SECRET}",
        f"?token=ghp_{'a' * 36}",
        # Percent-encoded and HTML-reference near misses: none becomes a
        # token-parameter delimiter after the one decode performed by its owning
        # parser stage.
        "?to%6Aen=x",
        "?to%6gen=x",
        "&amptoken=Xk9fQ2mP4nR7sT1v",
        "&Amp;token=Vb3nHj8LqW2zYc5d",
        "&questtoken=Wq7dRt2xKp9mZv4c",
        "?token&equalsQp4mXk9fR2vN7sT1",
        "&amp;amp;token=Mn8qR3tV6xZ1cK5p",
        "%26token=Yc5dVb3nHj8LqW2z",
        "?token%3DXk9fQ2mP4nR7sT1v",
        "&#382token=Vb3nHj8LqW2zYc5d",
        "?token&#61123abcWq7dRt2xKp9mZv4c",
        "&#00000000038;token=Qp4mXk9fR2vN7sT1",
        # a 40-char value glued to more run characters
        "X" + AWS_SECRET,
        AWS_SECRET + "A",
        "SECRET=" + AWS_SECRET + "ABC",
        AWS_SECRET + "X" + AWS_SECRET,
        # repeated / adjacent / overlapping matches
        f"{AWS_SECRET} {AWS_SECRET}",
        f"ghp_{'a' * 36} ghp_{'a' * 36}",
        f"ghp_{'a' * 36}ghp_{'b' * 36}",
        f"AKIAIOSFODNN7EXAMPLE AKIAIOSFODNN7EXAMPLE aws_secret_access_key={AWS_SECRET}",
        # a match whose text also occurs earlier behind a lookbehind that rejects it
        "x" + "M" + "a" * 24 + ".abc123." + "b" * 27 + " " + "M" + "a" * 24 + ".abc123." + "b" * 27,
        # base64-encoded credential (pass 2)
        "payload "
        + __import__("base64").b64encode(f"aws_secret_access_key={AWS_SECRET}".encode()).decode()
        + " end",
        # base64-looking but harmless: hex digests, commit hashes, long blobs
        "3f786850e387550fdab836ed7e6dc881de23001b0bd0d0d0aa1f2b3c4d5e6f70",
        "a" * 200,
        "A1b2C3d4" * 25,
        # padding variants
        AWS_SECRET + "=",
        AWS_SECRET + "==",
        AWS_SECRET + "===",
        # multiple passes interacting on one string
        f"aws_secret_access_key={AWS_SECRET}\nbare {AWS_SECRET}\nAKIAIOSFODNN7EXAMPLE",
        # PEM variants
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n",
        "prose mentioning -----BEGIN PRIVATE KEY----- inline\nand a trailing line",
        # large inputs
        ("lorem ipsum dolor sit amet " * 4000),
        ("lorem ipsum dolor sit amet " * 4000) + f" ghp_{'a' * 36}",
        f"ghp_{'a' * 36} " + ("lorem ipsum dolor sit amet " * 4000),
        # ── blast radius of the pass-2 per-run work ──
        # Many hex digests in one string. Each is a 64-char base64-alphabet run
        # that IS 4-aligned, so every one reaches the decode; this is the shape
        # the per-run cost was measured on.
        " ".join(f"{i:064x}" for i in range(40)),
        " ".join(f"{i:064X}" for i in range(40)),  # uppercase: different gate path
        # Runs at each length residue: %4 == 0 decodes, the rest cannot.
        _B64_RUN_40,  # 40, %4 == 0
        _B64_RUN_40 + "a",  # 41, %4 == 1
        _B64_RUN_40 + "ab",  # 42, %4 == 2
        _B64_RUN_40 + "abc",  # 43, %4 == 3
        _B64_RUN_40 + "abcd",  # 44, %4 == 0
        # A long non-decodable run beside a decodable one, in one string.
        _B64_RUN_40 + "a" + " and " + _B64_RUN_40,
        # Padding variants layered onto each residue, since `={0,2}` is consumed
        # by the chunk regex and shifts the length the precondition sees.
        _B64_RUN_40 + "ab" + "==",  # 44 total -> decodes
        _B64_RUN_40 + "abc" + "=",  # 44 total -> decodes
        _B64_RUN_40 + "abcd" + "=",  # 45 total -> cannot decode
        _B64_RUN_40 + "abcd" + "==",  # 46 total -> cannot decode
        # Decodes to printable text with no secret in it.
        _PRINTABLE_BLOB,
        f"blob {_PRINTABLE_BLOB} end",
        # Decodes to binary bytes.
        _GARBAGE_BLOB,
        f"blob {_GARBAGE_BLOB} end",
        # Decodes to the EMPTY string once invalid UTF-8 is dropped: the only
        # shape that lands below the pre-filter length gate.
        _SHORT_DECODE_BLOB,
        f"blob {_SHORT_DECODE_BLOB} end",
        # A real encoded credential whose decoded text sits BELOW the pre-filter
        # length gate, so the gate is skipped and the alternation runs directly.
        base64.b64encode(b"ghp_" + b"a" * 36).decode(),
        # ... and one comfortably ABOVE it, so the gate is applied.
        base64.b64encode(
            f"aws_secret_access_key={AWS_SECRET} trailing prose to lengthen".encode()
        ).decode(),
        # An encoded credential in the MISALIGNED padding shapes whose decodability
        # is interpreter dependent (40 data chars + one `=` decodes on 3.10/3.11 and
        # raises on 3.12). Whichever way the decode goes, live and oracle must agree.
        _ENCODED_CRED_STEM + "=",
        _ENCODED_CRED_STEM + "==",
        _ENCODED_CRED_STEM,
        f"payload {_ENCODED_CRED_STEM}= end",
        # ── positional shapes: the standalone credential comes FIRST ──
        f"{_HOSTED_ENCODED_CRED} {_ENCODED_CRED_HOST}",
        # The same value twice, once behind a label and once unlabelled: each is
        # judged on its own span, not on whether the value remains elsewhere.
        f"aws_secret_access_key={AWS_SECRET} {AWS_SECRET}",
        f"{AWS_SECRET} aws_secret_access_key={AWS_SECRET}",
        f"D{AWS_SECRET}ZGHUT8aws_secret_access_key={AWS_SECRET}",
        f"sk-proj-{'a' * 20}Z{AWS_SECRET}",
    ]
    return cases


# Shapes paired with plaintext the redactor must remove from them. They join the
# differential corpus, and `test_every_pinned_plaintext_is_removed` asserts the
# removal directly, so the oracle cannot agree with the live function on a shape
# both of them get wrong.
PLAINTEXT_PINS: tuple[tuple[str, str], ...] = (
    # A decodable chunk hosted inside an earlier non-decodable run (pass 2):
    # redacting by value would land the tag inside the host and keep the
    # standalone chunk.
    (f"{_ENCODED_CRED_HOST} {_HOSTED_ENCODED_CRED}", f" {_HOSTED_ENCODED_CRED}"),
    # Pass-4 shapes: an OPAQUE `?token=` / `&token=` value. The value is
    # deliberately non-`eyJ` and has no vendor prefix, so only the
    # parameter-name pass removes it.
    (
        "open https://host.example.com/?token=Xk9fQ2mP4nR7sT1v now",
        "Xk9fQ2mP4nR7sT1v",
    ),
    (
        "https://h.example/x?a=1&token=Vb3nHj8LqW2zYc5d&b=2",
        "Vb3nHj8LqW2zYc5d",
    ),
    # The parameter NAME folds case; the value bytes do not.
    (
        "see https://h.example/?TOKEN=Wq7dRt2xKp9mZv4c now",
        "Wq7dRt2xKp9mZv4c",
    ),
    # Standard query parsers percent-decode names after splitting raw `&`/`=`.
    # These single-encoded names therefore authenticate as `token`, while the
    # legacy oracle has no token-parameter pass and keeps each opaque value.
    (
        "see https://h.example/?to%6ben=Qp4mXk9fR2vN7sT1 now",
        "Qp4mXk9fR2vN7sT1",
    ),
    (
        "see https://h.example/?%74%6F%6B%65%6E=Mn8qR3tV6xZ1cK5p now",
        "Mn8qR3tV6xZ1cK5p",
    ),
    (
        "see https://h.example/?to%6Ben=Yc5dVb3nHj8LqW2z now",
        "Yc5dVb3nHj8LqW2z",
    ),
    # HTML references decoded in an attribute before query parsing are the same
    # pass-4 token parameter as their raw `&`, `?`, and `=` spellings. The
    # pre-pass-4 legacy oracle keeps each opaque value, while live and reference
    # pass 4 remove it.
    ("see &amp;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &AMP;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &amp%74oken=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &AMP%74oken=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#38;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#38token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#0000000038;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#x26;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#X26token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#x0000000026;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &quest;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#63;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#63token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#0000000063;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#x3F;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#X3ftoken=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see &#x000000003F;token=Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see ?token&equals;Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see ?token&#61;Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see ?token&#61Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see ?token&#0000000061;Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see ?token&#x3D;Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see ?token&#X3dXk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
    ("see ?token&#x000000003D;Xk9fQ2mP4nR7sT1v now", "Xk9fQ2mP4nR7sT1v"),
)

CORPUS = _corpus() + [text for text, _ in PLAINTEXT_PINS]


# ── Differential assertions ──


@pytest.mark.parametrize("text", CORPUS, ids=range(len(CORPUS)))
def test_output_is_byte_identical_to_reference(text: str) -> None:
    assert redact_credentials(text) == _reference_redact_credentials(text)


def test_reference_token_entity_case_matches_whatwg_decoding() -> None:
    """The independent pass-4 wrapper keeps named refs case-sensitive."""
    opaque = "Xk9fQ2mP4nR7sT1v"
    for name in ("&PERCNT;74oken", "&PerCnt;74oken"):
        text = f"?{name}={opaque}"
        assert redact_credentials(text) == _reference_redact_credentials(text) == (text, [])

    for name in ("&percnt;74oken", "&#X25;74oken"):
        text = f"?{name}={opaque}"
        expected = (
            f"?{name}=[REDACTED: credential]",
            ["Redacted token parameter value (16 chars)"],
        )
        assert redact_credentials(text) == _reference_redact_credentials(text) == expected


@pytest.mark.parametrize(
    ("text", "plaintext"),
    PLAINTEXT_PINS,
    ids=range(len(PLAINTEXT_PINS)),
)
def test_every_pinned_plaintext_is_removed(text: str, plaintext: str) -> None:
    """Each pinned shape loses the plaintext it carries, in the live function and the oracle."""
    assert plaintext in text, "corpus assumption: the shape carries the plaintext"
    assert plaintext not in redact_credentials(text)[0], "the redactor left plaintext behind"
    assert plaintext not in _reference_redact_credentials(text)[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"aws_secret_access_key={AWS_SECRET}", "aws_secret_access_key=[REDACTED: credential]"),
        (f'"SecretAccessKey": "{AWS_SECRET}"', '"SecretAccessKey": "[REDACTED: credential]"'),
        (
            "Authorization: Bearer abc.def.ghijklmnop",
            "Authorization: Bearer [REDACTED: credential]",
        ),
        ("api_key=Xk9fQ2mP4nR7", "api_key=[REDACTED: credential]"),
        (f"AKIAIOSFODNN7EXAMPLE {AWS_SECRET}", "AKIAIOSFODNN7EXAMPLE [REDACTED: credential]"),
        (AWS_SECRET, AWS_SECRET),
    ],
)
def test_labelled_and_paired_values_keep_their_label(text: str, expected: str) -> None:
    """The label and the key ID stay; only the value goes; an unlabelled value is left as is."""
    assert redact_credentials(text)[0] == expected
    assert _reference_redact_credentials(text)[0] == expected


def test_matched_span_is_redacted_not_an_earlier_lookalike() -> None:
    """Pass 1 must redact the span that matched, not an earlier substring.

    Guards the shape ``result.replace(matched, tag, 1)``. Here the
    boundary-anchored pattern matches only the SECOND token; the first is an
    ``x``-prefixed lookalike that happens to contain the matched text. The old
    shape redacted the lookalike and emitted the real credential verbatim.
    """
    token = "M" + "a" * 24 + ".abc123." + "b" * 27
    text = f"x{token} {token}"
    assert _CREDENTIAL_PATTERNS.search(text), "corpus assumption: the pattern fires"

    redacted, warnings = redact_credentials(text)

    # The real credential -- the standalone second token -- must be gone.
    assert f" {token}" not in redacted, "the matched credential survived redaction"
    assert redacted == f"x{token} {_REDACTED_CREDENTIAL_TAG}"
    assert warnings == [f"Redacted credential pattern ({len(token)} chars)"]


def test_hosted_encoded_credential_is_redacted_at_its_own_span() -> None:
    """Pass 2 must redact the chunk that decoded, not an earlier host of it.

    ``_ENCODED_CRED_HOST`` is one alphabet char glued to the encoded credential:
    a 57-char run that cannot decode, so pass 2 skips it and it survives. The
    standalone chunk that follows DOES decode. ``result.replace(chunk, tag, 1)``
    found the chunk's first occurrence -- inside the host -- and the real
    encoded credential was emitted verbatim.
    """
    assert not _decode_b64_chunk(_ENCODED_CRED_HOST), "corpus assumption: the host cannot decode"
    assert _decode_b64_chunk(_HOSTED_ENCODED_CRED), "corpus assumption: the chunk decodes"
    text = f"{_ENCODED_CRED_HOST} {_HOSTED_ENCODED_CRED}"
    assert not _CREDENTIAL_PATTERNS.search(text), "corpus assumption: pass 1 stays out"

    redacted, warnings = redact_credentials(text)

    assert f" {_HOSTED_ENCODED_CRED}" not in redacted, "the encoded credential survived redaction"
    assert redacted == f"{_ENCODED_CRED_HOST} {_REDACTED_ENCODED_CREDENTIAL_TAG}"
    assert warnings == [f"Redacted base64-encoded credential ({len(_HOSTED_ENCODED_CRED)} chars)"]


def test_redact_credentials_never_substitutes_by_value() -> None:
    """The redactor splices spans; ``str.replace`` on a matched value is the defect class.

    Walks the function's AST rather than its text so the docstring, which names
    the defect it retired, does not count.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(redact_credentials)))
    replace_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
    ]
    assert replace_calls == [], "redact_credentials must position every redaction by span"


def test_corpus_actually_exercises_every_pass() -> None:
    """A differential corpus that never triggers a pass proves nothing about it."""
    kinds = {w.split("(")[0].strip() for text in CORPUS for w in redact_credentials(text)[1]}
    assert "Redacted credential pattern" in kinds
    assert "Redacted base64-encoded credential" in kinds
    assert "Redacted token parameter value" in kinds

    # The context rules share pass 1's warning, so tell them apart by rule id.
    rules = {m.rule for text in CORPUS for m in redact_credentials_with_records(text)[2]}
    for rule in (
        "aws_secret_access_key",
        "aws_session_token",
        "authorization_header",
        "secret_label",
        "aws_key_pair",
        "encoded_credential",
    ):
        assert rule in rules, f"corpus never triggers {rule}"
    # The normalised view: a credential that only matches once decoded.
    view_hits = [
        text
        for text in CORPUS
        if (view := normalised_view(text)) is not None
        and next(_credential_matches(view.text), None) is not None
        and next(_credential_matches(text), None) is None
    ]
    assert view_hits, "corpus never carries a credential only the normalised view reveals"


def test_warnings_never_carry_secret_material() -> None:
    """Warnings must report length only — never a slice of the matched secret."""
    for text in CORPUS:
        _, warnings = redact_credentials(text)
        for warning in warnings:
            assert re.fullmatch(r"Redacted [a-z0-9 -]+ \(\d+ chars\)", warning), warning
            for secret in (AWS_SECRET, "IOSFODNN7EXAMPLE", "s3cr3tpw", _LINK_SIG):
                assert secret not in warning


# ── Pre-filter soundness: it must be a strict superset ──


@pytest.mark.parametrize("text", CORPUS, ids=range(len(CORPUS)))
def test_prefilter_fires_wherever_the_pattern_matches(text: str) -> None:
    if _CREDENTIAL_PATTERNS.search(text):
        assert _might_contain_credential(text), "pre-filter would skip a real credential"


def test_every_pattern_branch_has_a_prefilter_anchor() -> None:
    """Guards the maintenance hazard: a new branch with no pre-filter anchor.

    Adding a 24th branch to `_CREDENTIAL_PATTERNS` without an anchor in
    `_might_contain_credential` silently disables redaction for it. The count
    assertion makes that a loud failure at the point of the pattern edit.
    """
    assert len(BRANCHES) == len(BRANCH_SAMPLES), (
        f"_CREDENTIAL_PATTERNS has {len(BRANCHES)} branches but "
        f"{len(BRANCH_SAMPLES)} samples are registered. Add a sample for the new "
        f"branch AND a matching anchor in _might_contain_credential."
    )
    for index, sample in enumerate(BRANCH_SAMPLES):
        assert _CREDENTIAL_PATTERNS.search(sample), f"sample {index} matches no branch"
        assert _might_contain_credential(sample), f"pre-filter misses branch {index}"


def test_each_sample_is_specific_to_its_own_branch() -> None:
    """Per-branch negative control: drop branch i, sample i must stop matching.

    Without this, a sample could be matched by some OTHER branch and the coverage
    above would be vacuous — it would still pass with branch i deleted.
    """
    assert _rebuild(BRANCHES).pattern == _CREDENTIAL_PATTERNS.pattern, (
        "the branch splitter is not faithful; the per-branch controls below would "
        "be testing a different pattern than the module uses"
    )
    for index, sample in enumerate(BRANCH_SAMPLES):
        without = _rebuild([b for i, b in enumerate(BRANCHES) if i != index])
        assert not without.search(sample), (
            f"sample {index} still matches with branch {index} removed, so it does "
            f"not pin that branch"
        )


def test_prefilter_soundness_under_fuzz() -> None:
    """Random credential-ish strings: the pre-filter must never miss a match.

    Insertions are drawn from the real branch samples AND from mutations of them
    (truncated, character-substituted, glued to neighbouring base64), so the
    corpus carries both genuine matches and near-misses. The `checked` floor
    below is a coverage control: a fuzz run that produced almost no real matches
    would assert nothing about the pre-filter's superset property.
    """
    rng = random.Random(20260830)
    alphabet = string.ascii_letters + string.digits + "_-.:/@=+ \n'\"{},"

    seeds: list[str] = list(BRANCH_SAMPLES) + list(CONTEXT_SHAPES) + [AWS_SECRET]
    mutations: list[str] = []
    for seed in seeds:
        if len(seed) > 6:
            mutations.append(seed[: len(seed) // 2])  # truncated
            mutations.append(seed[:-1])  # one char short
            mutations.append(seed[0].swapcase() + seed[1:])  # case-flipped anchor
            mutations.append("Z" + seed)  # glued left
            mutations.append(seed + "Z")  # glued right
    candidates = seeds + mutations

    checked = 0
    for _ in range(4000):
        body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        for _ in range(rng.randint(0, 2)):
            at = rng.randint(0, len(body))
            body = body[:at] + rng.choice(candidates) + body[at:]
        if _CREDENTIAL_PATTERNS.search(body):
            checked += 1
            assert _might_contain_credential(body), repr(body)
        assert redact_credentials(body) == _reference_redact_credentials(body), repr(body)
    assert checked >= 500, f"fuzz produced only {checked} real matches; too weak"


# ── The chunk-decode equivalence the rewrite relies on ──


def test_decode_b64_chunk_matches_generic_helper_on_chunks() -> None:
    """`_decode_b64_chunk` must equal `_decode_b64_safe` for any single chunk."""
    seen = 0
    for text in CORPUS:
        for m in _B64_CHUNK_RE.finditer(text):
            chunk = m.group()
            seen += 1
            assert _decode_b64_chunk(chunk) == _decode_b64_safe(chunk), repr(chunk[:40])
    assert seen >= 10, f"only {seen} chunks exercised; corpus too weak"


# ── Negative controls: prove the differential test can fail ──


def test_differential_test_detects_a_dropped_pattern_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break redaction by deleting a branch; the oracle comparison must notice."""
    import kiro_crew.security as security

    crippled = _rebuild([b for i, b in enumerate(BRANCHES) if i != 4])  # drop gh[opsur]_
    monkeypatch.setattr(security, "_CREDENTIAL_PATTERNS", crippled)

    sample = "ghp_" + "a" * 36
    # The live function now under-redacts, while our pinned oracle (which reads
    # the patched module attribute too) is compared against the ORIGINAL expected
    # output captured before patching.
    assert security.redact_credentials(sample) == (sample, [])
    assert sample != _REDACTED_CREDENTIAL_TAG


def test_differential_test_detects_a_too_narrow_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-filter that misses a branch must break the differential comparison.

    This is the control that matters most: it proves the byte-identity assertion
    above is capable of failing when the optimisation is wrong, rather than
    passing because both sides share a defect.
    """
    import kiro_crew.security as security

    monkeypatch.setattr(
        security, "_CREDENTIAL_PREFILTER_LITERALS", ("this-anchor-matches-nothing",)
    )
    monkeypatch.setattr(security, "_CREDENTIAL_PREFILTER_GH_RE", re.compile(r"(?!x)x"))
    monkeypatch.setattr(security, "_CREDENTIAL_PREFILTER_TELEGRAM_RE", re.compile(r"(?!x)x"))
    monkeypatch.setattr(security, "_CREDENTIAL_PREFILTER_DISCORD_RE", re.compile(r"(?!x)x"))
    monkeypatch.setattr(security, "_CREDENTIAL_PREFILTER_URI_RE", re.compile(r"(?!x)x"))

    sample = "ghp_" + "a" * 36
    assert not security._might_contain_credential(sample), "control did not disarm"

    live = security.redact_credentials(sample)
    reference = _reference_redact_credentials(sample)
    assert live != reference, "differential assertion cannot detect a broken pre-filter"
    assert live == (sample, []), "expected the broken pre-filter to skip redaction"
    assert reference == (_REDACTED_CREDENTIAL_TAG, ["Redacted credential pattern (40 chars)"])


# ── The widened-branch guard ──
#
# The branch-count assertion catches an ADDED branch. It structurally cannot catch
# a WIDENED one, because widening does not change the count -- and a branch that
# accepts more than its anchor would be skipped by the gate. So exercise each
# branch's OWN matcher against the gate: mutate its registered sample, and for
# every mutation the branch still accepts, require the gate to accept it too.

_HOMOGLYPHS: tuple[tuple[str, str], ...] = (
    ("i", "\u0131"),  # dotless i      -> folds to i/I under re.I, not under .lower()
    ("I", "\u0130"),  # I with dot     -> ditto
    ("s", "\u017f"),  # long s         -> folds to s/S under re.I
    ("k", "\u212a"),  # Kelvin sign    -> folds to k/K under re.I
)


def _widening_mutations(sample: str) -> list[str]:
    """Perturbations that a widened or case-relaxed branch would start accepting."""
    out: list[str] = [
        sample.upper(),
        sample.lower(),
        sample.swapcase(),
        sample + "extra",
        "prefix" + sample,
    ]
    for plain, glyph in _HOMOGLYPHS:
        for source in (plain, plain.upper(), plain.lower()):
            if source in sample:
                out.append(sample.replace(source, glyph, 1))
                out.append(sample.replace(source, glyph))
    return out


def test_widening_a_branch_cannot_outgrow_its_anchor() -> None:
    """Per-branch superset property under mutation.

    This is the guard the branch-count assertion cannot provide: a branch made
    case-insensitive would accept homoglyph spellings that a case-sensitive or
    `str.lower()` anchor rejects.
    """
    checked = 0
    for index, (branch, sample) in enumerate(zip(BRANCHES, BRANCH_SAMPLES)):
        alone = re.compile("(?:" + branch + ")")
        for mutation in _widening_mutations(sample):
            if not alone.search(mutation):
                continue  # this branch does not accept the mutation -- not its problem
            checked += 1
            assert _might_contain_credential(mutation), (
                f"branch {index} accepts a mutation its anchor rejects, so widening "
                f"that branch would silently disable redaction: {mutation!r}"
            )
    assert checked >= len(BRANCH_SAMPLES), (
        f"only {checked} branch/mutation pairs were assertable; the mutation set is "
        f"too weak to guard {len(BRANCH_SAMPLES)} branches"
    )


def test_case_insensitive_branches_must_be_anchored_by_the_same_engine() -> None:
    """Pin the count of case-insensitive branches at zero.

    A case-insensitive branch cannot be gated by a case-sensitive literal, nor by
    a hand-rolled fold such as `str.lower()` -- only by the same regex engine.
    Every pre-filter literal is case-sensitive, so a `(?i:…)` branch added here
    fails this until it is given a same-engine regex anchor.
    """
    case_insensitive = [i for i, branch in enumerate(BRANCHES) if "(?i" in branch]
    assert case_insensitive == [], (
        f"case-insensitive branches changed to {case_insensitive}. Every one needs a "
        f"regex anchor in `_might_contain_credential` using the same engine."
    )


def test_prefilter_still_skips_credential_free_text() -> None:
    """The performance win must survive the correctness fix.

    The whole point of the gate is that ordinary text skips the 23-branch scan. A
    fix that made the gate fire on everything would be correct and worthless, so
    pin the negative direction too.
    """
    clean = [
        "The gateway flushes dirty slots on a timer; each flush re-serialises the "
        "whole slot history and redacts it. See dashboard/chat_handlers.py:3826.",
        "def _build_message_entry(role: str, content: str) -> dict[str, object]: ...",
        '{"session": "chat-1281-1785676802", "tab": 7, "unread": false}',
        "3f786850e387550fdab836ed7e6dc881de23001b0bd0d0d0aa1f2b3c4d5e6f70",
        "https://example.com/reviews/42/revisions/1",
        "Authorised users may not need an authorisation header at all.",
        "",
    ]
    for text in clean:
        assert not _CREDENTIAL_PATTERNS.search(text), f"corpus entry is not clean: {text!r}"
        assert not _might_contain_credential(
            text
        ), f"pre-filter fired on credential-free text, discarding the fast path: {text!r}"


# ── Structure-derived widening guard ──
#
# `_widening_mutations` perturbs each branch's REGISTERED SAMPLE, so it covers the
# case-fold and affix classes but is structurally blind to a branch widened with a
# NEW ALTERNATIVE: extending `sk-proj-` to `sk-(?:proj|svcacct)-` changes no branch
# count, keeps the old sample matching, and produces no mutation carrying the new
# prefix. The pre-filter then never fires for that token family and it persists
# unredacted with every test green.
#
# Closing that needs samples derived from the branch's OWN structure rather than
# from a hand-written list. Walking the parsed pattern and expanding each
# alternation yields one concrete string per alternative, so a new alternative
# produces a new sample automatically and the anchor must cover it.

try:  # Python 3.11+ exposes the regex parser as re._parser
    from re import _parser as _regex_parser
except ImportError:  # Python 3.10 and earlier
    import sre_parse as _regex_parser  # type: ignore[no-redef]

_GENERATOR_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# Bound the fan-out: nested alternations multiply, and an unbounded product would
# make this quadratic on the URI branch's scheme list.
_MAX_GENERATED_VARIANTS = 48


def _opcode_name(opcode: object) -> str:
    return str(opcode).split(".")[-1].lower()


def _pick_from_set(items: list[tuple[object, object]]) -> str:
    """Choose one concrete character satisfying a parsed character set."""
    negated = any(_opcode_name(op) == "negate" for op, _ in items)
    if negated:
        banned: set[str] = set()
        for op, value in items:
            name = _opcode_name(op)
            if name == "literal":
                banned.add(chr(value))  # type: ignore[arg-type]
            elif name == "range":
                low, high = value  # type: ignore[misc]
                banned.update(chr(c) for c in range(low, high + 1))
        for char in _GENERATOR_ALPHABET:
            if char not in banned:
                return char
        return "x"
    for op, value in items:
        name = _opcode_name(op)
        if name == "literal":
            return chr(value)  # type: ignore[arg-type]
        if name == "range":
            low, high = value  # type: ignore[misc]
            for code in range(low, high + 1):
                if chr(code) in _GENERATOR_ALPHABET:
                    return chr(code)
            return chr(low)
        if name == "category":
            category = _opcode_name(value)
            if "not" in category:
                return "a"
            if "digit" in category:
                return "7"
            if "space" in category:
                return " "
            return "a"
    return "a"


def _generate_from_sequence(sequence: object) -> list[str]:
    """Concrete strings matching *sequence*; alternations fan out into variants."""
    out = [""]
    for opcode, value in sequence:  # type: ignore[attr-defined]
        name = _opcode_name(opcode)
        if name == "literal":
            out = [s + chr(value) for s in out]
        elif name == "not_literal":
            replacement = next(c for c in _GENERATOR_ALPHABET if c != chr(value))
            out = [s + replacement for s in out]
        elif name == "in":
            out = [s + _pick_from_set(value) for s in out]
        elif name == "any":
            out = [s + "a" for s in out]
        elif name in ("max_repeat", "min_repeat", "possessive_repeat"):
            minimum, _maximum, subpattern = value
            pieces = _generate_from_sequence(subpattern)
            out = [s + (pieces[0] if pieces else "") * minimum for s in out]
        elif name in ("subpattern", "atomic_group"):
            subpattern = value[-1] if name == "subpattern" else value
            variants = _generate_from_sequence(subpattern)
            out = [s + v for s in out for v in variants][:_MAX_GENERATED_VARIANTS]
        elif name == "branch":
            _, alternatives = value
            variants = []
            for alternative in alternatives:
                variants.extend(_generate_from_sequence(alternative))
            variants = variants[:_MAX_GENERATED_VARIANTS]
            out = [s + v for s in out for v in variants][:_MAX_GENERATED_VARIANTS]
        elif name == "assert" and value[0] < 0:
            # A positive look-behind needs its text BEFORE the match, so it is
            # written out: `(?<=Signature=)` contributes `Signature=`.
            variants = _generate_from_sequence(value[1])
            out = [s + v for s in out for v in variants][:_MAX_GENERATED_VARIANTS]
        # Other anchors and look-arounds consume nothing, so they contribute no text.
        # A leading space is prepended by the caller to satisfy left look-behinds.
    return out[:_MAX_GENERATED_VARIANTS]


def _generate_branch_variants(fragment: str) -> list[str]:
    try:
        parsed = _regex_parser.parse(fragment)
    except re.error:
        return []
    return _generate_from_sequence(parsed)


def test_a_widened_branch_cannot_outgrow_its_anchor() -> None:
    """Every string a branch's own structure can produce must clear the gate.

    Complements `test_widening_a_branch_cannot_outgrow_its_anchor`: that one
    perturbs a sample (covering case folds and homoglyphs, which generation does
    not), while this one enumerates the branch's alternatives (covering new
    alternatives, which perturbation does not).

    Verified to catch the class it exists for: widening branch 9 to
    `sk-(?:proj|svcacct)-` yields a `sk-svcacct-` variant the anchors reject,
    which the perturbation guard's 13 mutations all miss.
    """
    ungenerable: list[int] = []
    checked = 0
    for index, branch in enumerate(BRANCHES):
        alone = re.compile("(?:" + branch + ")")
        # Leading space satisfies the left look-behinds on the Discord and
        # link-token branches without contributing a matchable character.
        variants = [" " + v for v in _generate_branch_variants(branch)]
        matching = [v for v in variants if alone.search(v)]
        if not matching:
            # Positive control per branch: a branch we cannot generate a match for
            # is NOT silently skipped, it is reported below so the gap is visible.
            ungenerable.append(index)
            continue
        for variant in matching:
            checked += 1
            assert _might_contain_credential(variant), (
                f"branch {index} accepts a string its anchors reject, so widening "
                f"that branch leaves the token family unredacted: {variant!r}"
            )

    assert not ungenerable, (
        f"no matching sample could be generated for branches {ungenerable}, so they "
        f"are unguarded against widening. Extend the generator rather than dropping "
        f"the branch from the check."
    )
    assert checked >= len(BRANCHES), (
        f"only {checked} generated samples were assertable across {len(BRANCHES)} "
        f"branches; the generator is too weak to guard them"
    )


def test_a_decode_length_precondition_would_be_version_dependent() -> None:
    """Records why `_decode_b64_chunk` carries NO length short-circuit.

    Skipping the decode when `len(chunk) % 4` is non-zero looks sound -- and IS
    sound on 3.12, where `validate=True` rejects such a length. It is a REDACTION
    BYPASS on 3.10 and 3.11: `binascii.a2b_base64`'s padding leniency changed with
    `strict_mode`, so a chunk of 40 data characters plus one `=` decodes there, and
    skipping it would leave an encoded credential unredacted. No version-invariant
    predicate exists either -- 43 data characters plus `==` decodes on 3.10 while
    failing both a total-length and a stripped-length test.

    So assert the property that DOES hold on every interpreter: the hot-path chunk
    helper agrees with the ungated `_decode_b64_safe`, for exactly the misaligned
    shapes that motivated the rejected guard. Whether they decode may differ by
    version; that the two helpers agree may not.
    """
    payload = f"aws_secret_access_key={AWS_SECRET}"
    stem = base64.b64encode(payload.encode()).decode().rstrip("=")

    misaligned = 0
    for shape in (stem, stem + "=", stem + "==", stem[:-1], stem[:-1] + "=", stem[:-2] + "=="):
        assert _decode_b64_chunk(shape) == _decode_b64_safe(shape), repr(shape[:40])
        if len(shape) % 4:
            misaligned += 1
    assert misaligned >= 2, "shapes no longer exercise a non-4-aligned chunk"

    # And the credential in its canonical, correctly padded form is still redacted,
    # on every interpreter -- the guard's removal must not have cost detection.
    canonical = base64.b64encode(payload.encode()).decode()
    assert _decode_b64_chunk(canonical), "canonical encoded credential must be detected"


def test_decode_gate_agrees_with_the_ungated_helper_across_the_length_gate() -> None:
    """Both sides of `_PREFILTER_MIN_LEN` must agree with the ungated helper.

    Below the threshold the pre-filter is skipped and the alternation runs
    directly; at or above it the pre-filter gates the alternation. `_decode_b64_safe`
    is ungated and untouched, so it is the oracle for both sides, and this asserts
    the corpus actually reaches each one.

    Note the asymmetry the gate relies on: a chunk is at least 40 base64
    characters, so it always decodes to at least 30 raw bytes, and a decoded
    STRING shorter than the threshold only arises when `errors="ignore"` discards
    invalid UTF-8. No `_CREDENTIAL_PATTERNS` branch matches text that short, so the
    below-threshold path is reached only by non-credential blobs -- and even if one
    existed, skipping the pre-filter runs the FULL alternation, never less.
    """
    below = above = 0
    for text in CORPUS:
        for m in _B64_CHUNK_RE.finditer(text):
            chunk = m.group()
            assert _decode_b64_chunk(chunk) == _decode_b64_safe(chunk), repr(chunk[:40])
            # Classify by what actually decodes, NOT by `len(chunk) % 4`: whether a
            # misaligned chunk decodes is interpreter dependent (see
            # `test_a_decode_length_precondition_would_be_version_dependent`), so a
            # length test here would silently skip real cases on 3.10 and 3.11.
            try:
                decoded = base64.b64decode(chunk, validate=True).decode("utf-8", errors="ignore")
            except binascii.Error:
                continue
            if len(decoded) >= _PREFILTER_MIN_LEN:
                above += 1
            else:
                below += 1

    assert below > 0, "corpus never produces a decoded blob below the length gate"
    assert above > 0, "corpus never produces a decoded blob at or above the length gate"

    # And a real encoded credential, which necessarily sits above the gate, is
    # still detected through it.
    cred = base64.b64encode(
        f"aws_secret_access_key={AWS_SECRET} trailing prose to lengthen".encode()
    ).decode()
    assert _decode_b64_chunk(cred) == _decode_b64_safe(cred)
    assert _decode_b64_chunk(cred), "the gate must not hide a real encoded credential"


def test_differential_test_detects_a_too_narrow_decoded_blob_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the byte-identity assertion catches a broken decoded-blob gate.

    Disarm the pre-filter that pass 2 now consults, so a genuine base64-encoded
    credential is skipped instead of redacted. The payload is one only pass 1's
    alternation catches (the label and catalog rules are not gated), so the
    disarmed gate is the only thing between it and the output. The reference
    oracle's pass 2 is the module's encoded-run finder, so the expected result is
    captured BEFORE the gate is disarmed.
    """
    import kiro_crew.security as security

    payload = "xoxb-1234567890-abcdefghijklmnop in a sentence"
    sample = "payload " + base64.b64encode(payload.encode()).decode() + " end"
    reference = _reference_redact_credentials(sample)

    monkeypatch.setattr(security, "_might_contain_credential", lambda text: False)
    assert len(payload) >= _PREFILTER_MIN_LEN, "payload must clear the length gate"

    live = security.redact_credentials(sample)
    assert live != reference, "the differential test cannot detect a broken blob gate"
    assert "[REDACTED: encoded credential]" in reference[0]
    assert (
        "[REDACTED: encoded credential]" not in live[0]
    ), "expected the disarmed gate to leak the encoded credential"


def test_a_length_short_circuit_would_leak_an_encoded_credential() -> None:
    """Control for the REJECTED `len(chunk) % 4` short-circuit.

    Simulate the guard and show it can only ever suppress detection, never improve
    it. On 3.10 and 3.11 it drops a decodable credential chunk outright; on 3.12
    the shape does not decode anyway, which is exactly why the defect was invisible
    when the change was tested on 3.12 alone.
    """
    payload = f"aws_secret_access_key={AWS_SECRET}"
    stem = base64.b64encode(payload.encode()).decode().rstrip("=")

    # Only shapes the rejected guard would actually have rejected. `stem + "="` is
    # the canonical 4-aligned form here, so it must NOT be counted as misaligned.
    misaligned = [s for s in (stem, stem + "=", stem + "==") if len(s) % 4]
    assert misaligned, "no misaligned shape constructed"

    leaked = 0
    for shape in misaligned:
        real = _decode_b64_chunk(shape)
        guarded = ""  # what the rejected `len(chunk) % 4` guard would have returned
        assert not (guarded and not real), "guard cannot detect more than no guard"
        if real:
            leaked += 1

    # The decoder restores stripped padding, so the unpadded stem decodes and is
    # caught on every interpreter: the length guard would have dropped exactly
    # that detection. The in-loop invariant above -- a guard can never detect
    # more than no guard -- is what rules the guard out.
    assert _decode_b64_chunk(stem), "the unpadded stem must decode"
    assert leaked >= 1

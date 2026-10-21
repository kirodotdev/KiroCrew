"""The package's redaction entry points: whole text, audit samples and streams.

:func:`redact_with_findings` is the one composed pass over the two baseline
passes (exfiltration URLs and credentials), built in
:mod:`kiro_crew.security.pass_compose`: neither order of two sequential passes is
right, so code outside this package calls it rather than sequencing the raw
passes. :func:`redact` and :func:`redact_and_truncate` call it.
:func:`redact_and_truncate` redacts BEFORE it truncates, so a secret straddling the
cut cannot leak as a fragment. :class:`StreamRedactor` redacts streamed output chunk
by chunk, holding back a trailing credential-class run until it is complete. Every
one is the companion-blind baseline: an egress site finishes the text through
``platform.context.redact_via_context``.

The patterns live in :mod:`kiro_crew.security.redaction` and
:mod:`kiro_crew.security.exfil`; this module sequences them. The holdback bounds
(``_STREAM_HOLDBACK_MAX`` and its siblings) and ``redact`` live beside
:class:`StreamRedactor`, their reader, because tests patch them on
``kiro_crew.security``.

``kiro_crew.security`` is the import path. This module reads the package's
``_submodule`` resolver, so the package imports it at its foot, once that is bound.
New redaction entry points belong here.
"""

from __future__ import annotations

import re
import string
from typing import TYPE_CHECKING

from kiro_crew import credential_values
from kiro_crew.security import _submodule

from . import pass_compose, redaction, stream_aws_anchor

if TYPE_CHECKING:
    from collections.abc import Callable


def redact_with_findings(text: str) -> tuple[str, list[str], list[str]]:
    """Apply all redaction passes, reporting what each one removed.

    Both passes find their spans on the original text and the text is rewritten
    once (see :mod:`kiro_crew.security.pass_compose`); neither order of two
    sequential passes is right. Call this instead of hand-sequencing them.

    Returns ``(text, credential_warnings, url_warnings)``. Both lists hold the
    WARNING strings the underlying passes report (``"Redacted credential pattern
    (20 chars)"``), never the removed values, so a caller can tell the user their
    content was altered -- and log that fact -- without handling a secret.

    This is the companion-BLIND baseline, like every ``security.redact*`` entry
    point: it consults no active :class:`CredentialPolicy`. An egress site must
    finish the text through ``platform.context.redact_via_context`` so a loaded
    companion's extra patterns apply; use this one for the warnings, or where the
    baseline is deliberately the subject.
    """
    return pass_compose.compose(text)


def redact(text: str) -> str:
    """Apply all redaction passes (exfiltration URLs + credentials)."""
    return redact_with_findings(text)[0]


# ── Streaming redaction ──
# Per-chunk redaction misses a credential split across token/streaming
# boundaries: a chunk ending ``...AKIA`` and the next starting ``IOSFODNN7...``
# each individually escape redact_credentials(), so the raw fragments reach
# WebSocket/SSE consumers even though the final assembled message is redacted.
# StreamRedactor withholds the trailing run of "credential-class" characters
# (which could be the start of a not-yet-complete credential) until a
# terminator arrives or the stream ends, redacting only the confirmed-safe
# prefix before it is emitted on the wire.

# Characters that can appear inside a credential token/pattern. A credential is
# a contiguous run of these; any byte OUTSIDE this set terminates an in-progress
# match, so text up to (and including) such a terminator is safe to redact and
# emit. Includes URL / base64 / connection-string punctuation so exfil URLs and
# DB URIs are also held intact across chunk boundaries — plus quotes and URL
# query delimiters (``"`` ``'`` ``?&#``) so a JSON key/value or query-string
# secret is not committed piecemeal across a chunk edge. (The private-key HEADER
# phrase contains spaces and is the one pattern that can split on a terminator;
# it is a non-secret header string and the final full-text pass still redacts
# the persisted/displayed copy.)
_CRED_CLASS: frozenset[str] = frozenset(
    string.ascii_letters
    + string.digits
    + "_-+/=.:@%~"
    + '"'
    + "'"
    + "?&#"
    # A JSON escape, a control character and an invisible format character are
    # characters the batch pass reads THROUGH (``redaction_normalise``), so one
    # inside a token must not end its run either.
    + "\\"
    + "".join(chr(c) for c in range(0x20) if chr(c) not in "\t\n\r")
    + "\x7f\u00ad\u200b\u200c\u200d\u2060\ufeff"
)

#: A terminal escape sequence ending at the cut: the batch pass drops it whole, so
#: the trailing run continues through it.
_CSI_TAIL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]?\Z")
#: How far back a CSI sequence ending at the cut is looked for.
_CSI_REACH = 32


def _natural_cut(buf: str) -> int:
    """Start of the trailing run of credential-class characters, through escape sequences."""
    cut = len(buf)
    while cut > 0:
        if buf[cut - 1] in _CRED_CLASS:
            cut -= 1
            continue
        csi = _CSI_TAIL_RE.search(buf, max(0, cut - _CSI_REACH), cut)
        if csi is None:
            break
        cut = csi.start()
    return cut


# Upper bound on withheld trailing characters. Larger than the longest
# fixed-format credential so a split token is always rejoined before emission;
# bounds latency/memory for a pathologically long unbroken run (only affects a
# single >512-char secret with no delimiter, which no supported provider issues).
_STREAM_HOLDBACK_MAX = 512

# PEM header hold-back: matches an in-progress "BEGIN [type] PRIVATE KEY"
# phrase in the tail of the commit buffer.  When found, we refuse to commit
# at the whitespace boundary so the full multi-word marker stays inside one
# redaction pass (ported from the upstream project).
_PEM_HOLD_RE = re.compile(
    r"BEGIN[\s](?:RSA[\s]?|DSA[\s]?|EC[\s]?|OPENSSH[\s]?)?(?:PRIVATE)?[\s]?$",
    re.IGNORECASE,
)

# JWTs (esp. RS256/ES256 with embedded claims) routinely exceed the 512-char DoS
# floor, so a terminal JWT longer than _STREAM_HOLDBACK_MAX would be bisected by
# the default cap and emitted half-redacted. When the withheld tail *looks like*
# the start of a JWT, we raise the cap to this larger ceiling so the whole token
# is rejoined before emission while still keeping the buffer bounded.
_STREAM_HOLDBACK_JWT_MAX = 4096

#: The longest current line kept to judge a config line's start by.
_OUT_LINE_MAX = 256

#: How much committed text is kept for a catalog keyword the commit cut in two:
#: longer than any keyword.
_CATALOG_CONTEXT_TAIL = 64

#: A PEM private-key block still open (its header in, its ``-----END`` line not
#: yet): held whole as far as an 8192-bit RSA key runs, then dropped to its end.
_PEM_OPEN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_PEM_HOLD_REACH = 8192
#: An ``-----END`` line cut by the chunk is carried this far into the next pass.
_PEM_CARRY = 40

#: The deepest label indent a block-scalar drop is keyed by; a deeper one drops
#: as if at this indent (more, never less).
_BLOCK_INDENT_MAX = 40
#: A line break and the indentation after it, at the buffer end, inside a
#: block-scalar drop.
_BLOCK_PENDING_RE = re.compile(r"\n[ ]*")

# A sticky discard consumes only bytes that the ARMING anchor defines as value
# bytes. These two classes are the existing classes from the partial JWT and
# Bearer anchors below, named so the anchors and discard cannot drift apart.
_JWT_SEGMENT_VALUE_CLASS = r"[A-Za-z0-9_-]"
_BEARER_VALUE_CLASS = r"[A-Za-z0-9._~+/=-]"
_STREAM_DISCARD_RUN_RES = {
    # Read off the owner at import: the class is baked into the compiled pattern
    # here, so this is a one-shot build input rather than a stored re-export, read
    # through the ``redaction`` binding imported above.
    "token-param": re.compile(rf"{redaction._TOKEN_PARAM_VALUE_CLASS}*"),
    "jwt": re.compile(rf"(?:{_JWT_SEGMENT_VALUE_CLASS}|\.)*"),
    "bearer": re.compile(rf"{_BEARER_VALUE_CLASS}*"),
    "aws-keyed": re.compile(rf"{stream_aws_anchor.VALUE_CLASS}*"),
    # A labelled quoted value past its hold reach: everything to its closing
    # quote or the line end (escapes included), as the batch rule reads it.
    "quoted-dq": re.compile(r'(?:[^"\\\n]|\\.)*'),
    # A doubled single quote (``'it''s'``) is one quote inside the value.
    "quoted-sq": re.compile(r"(?:[^'\\\n]|\\.|'')*"),
    # The same value inside an escaped string (``{\\"password\\": \\"...``): it
    # ends at the escaped quote.
    # One level deeper, the value's own escapes are doubled: ``\\\\\\\\`` is its
    # escaped backslash, ``\\\\\\"`` its escaped quote, and the ``\\"`` left over
    # is the quote that closes it.
    "quoted-esc-dq": re.compile(r'(?:[^\\\n]|\\{4}|\\{3}"|\\{2}(?=[^"\\])|\\(?=[^"\\]))*'),
    "quoted-esc-sq": re.compile(r"(?:[^\\\n]|\\{4}|\\{3}'|\\{2}(?=[^'\\])|\\(?=[^'\\]))*"),
    # Opened by a percent-encoded quote: through the same encoded quote.
    "quoted-pct-dq": re.compile(r"(?:(?!%22)[^%\n]|%(?!22))*(?:%22)?", re.IGNORECASE),
    "quoted-pct-sq": re.compile(r"(?:(?!%27)[^%\n]|%(?!27))*(?:%27)?", re.IGNORECASE),
    # Opened by a quote escaped more than once (a value three JSON levels deep):
    # dropped to the line end, fail closed.
    "quoted-line": re.compile(r"[^\n]*"),
    # A PEM private key past its hold reach: through its ``-----END ...`` line.
    "pem": re.compile(
        r"(?:[^-]|-(?!----END [A-Z ]*PRIVATE KEY-----))*(?:-----END [A-Z ]*PRIVATE KEY-----)?"
    ),
    # A labelled XML value past its hold reach: up to its closing tag's ``<``.
    "xml-value": re.compile(r"[^<\n]*"),
    # A YAML block scalar past its hold reach, opened by a label indented N
    # spaces (``block-N``): the rest of its line, then every line indented deeper
    # or blank. A line cut by the chunk while still only indentation is carried
    # (``_BLOCK_PENDING_RE``) until it shows which it is.
    **{
        f"block-{n}": re.compile(rf"[^\n]*(?:\n(?:[ ]{{{n + 1},}}[^\n]*|[ \t]*(?=\n)))*")
        for n in range(_BLOCK_INDENT_MAX + 1)
    },
    # An unquoted value behind an encoded label: the label rules' value class.
    "view-unquoted": re.compile(rf"{stream_aws_anchor.VALUE_CLASS}*"),
    # A vendor value or STS token past its hold reach: to the next blank or quote.
    "long-token": re.compile(r"[^\s\"'`<>]*"),
    # A base64 run (standard or URL-safe) that decodes to a secret.
    "encoded-run": re.compile(
        r"(?:[A-Za-z0-9+/_-]|(?:\r?\n|\\n)"
        r"(?=[A-Za-z0-9+/_-]{16}|[A-Za-z0-9+/_-]{4,15}={0,2}(?:\r?\n|\\n)))*"
        r"={0,2}"
    ),
}
#: How many characters lengthening one base64 run the stream gathers before it
#: reads the buffer again.
_STREAM_COALESCE = 64
#: How much the stream must already hold before any delta is gathered.
_STREAM_COALESCE_HELD = 256
_B64_RUN_CHARS = frozenset(string.ascii_letters + string.digits + "+/")

_B64_LINE_CHARS = frozenset(string.ascii_letters + string.digits + "+/_-=")
_LINE_BREAK_TAIL_RE = re.compile(r"\r?\n|\\n")
_ENCODED_BREAK_TAIL_RE = re.compile(r"(?:\r|\\|(?:\r?\n|\\n)[A-Za-z0-9+/_-]{0,15}={0,2}\r?)")


def _break_before(buf: str, pos: int) -> int:
    """Length of the line break ending at *pos*: a newline (``\\r\\n``, ``\\n``,
    a lone ``\\r``) or a literal ``\\n``; 0 when there is none."""
    if buf.endswith(("\r\n", "\\n"), 0, pos):
        return 2
    return 1 if pos > 0 and buf[pos - 1] in "\r\n" else 0


def _line_start(buf: str, pos: int) -> int:
    """Start of the base64 characters ending at *pos*; a literal ``\\n`` ends
    them, although its ``n`` is a base64 character."""
    while pos > 0 and buf[pos - 1] in _B64_LINE_CHARS:
        if buf[pos - 1] == "n" and pos > 1 and buf[pos - 2] == "\\":
            break
        pos -= 1
    return pos


def _encoded_tail_start(buf: str) -> int | None:
    """Where the base64 run reaching the end of *buf* starts, the run wrapped
    over lines (by newlines or a literal ``\\n``) included; ``None`` when the
    tail is no run of 40 or more characters. One backward pass."""
    end = len(buf) - _break_before(buf, len(buf))
    start = _line_start(buf, end)
    while start > 0:
        brk = _break_before(buf, start)
        if not brk:
            break
        line_end = start - brk
        line_start = _line_start(buf, line_end)
        if line_end - line_start < 16:
            break
        start = line_start
    run = sum(1 for c in buf[start:end] if c in _B64_LINE_CHARS)
    return start if run >= 40 else None


def _decodes_to_secret(run: str) -> bool:
    """Whether base64 *run* (line breaks dropped) decodes to a secret at any of
    its four alignments, cut to whole quanta: a run whose head was already
    released starts anywhere in a quantum."""
    joined = _LINE_BREAK_TAIL_RE.sub("", run).rstrip("=")
    decode = _submodule("redaction_decode")
    for skip in range(4):
        arrived = joined[skip : skip + (len(joined) - skip) // 4 * 4]
        if len(arrived) >= 40 and any(True for _span in decode.encoded_credential_spans(arrived)):
            return True
    return False


def _encoded_head_end(buf: str) -> int:
    """End of the base64 run *buf* starts with, wrapped lines included, one
    forward pass; it stops at a short line as :func:`_encoded_tail_start` does."""
    pos = 0
    while True:
        end = pos
        while end < len(buf) and buf[end] in _B64_LINE_CHARS:
            if buf.startswith("\\n", end - 1) and end > pos:
                end -= 1
                break
            end += 1
        brk = 2 if buf.startswith(("\r\n", "\\n"), end) else 1 if buf[end : end + 1] == "\n" else 0
        if end == len(buf) or (brk and end + brk >= len(buf)):
            return len(buf)  # still arriving: the run may go on
        nxt = end + brk
        line = nxt
        while line < len(buf) and buf[line] in _B64_LINE_CHARS:
            line += 1
        if brk and line == len(buf):
            return len(buf)  # its next line is still arriving
        if not brk or line - nxt < 16:
            return end
        pos = nxt


_QUOTED_DISCARD_KIND = {
    "%22": "quoted-pct-dq",
    "%27": "quoted-pct-sq",
    '"': "quoted-dq",
    "'": "quoted-sq",
    '\\"': "quoted-esc-dq",
    "\\'": "quoted-esc-sq",
}

# Bytes of terminator-less continuation dropped silently between two tags. It is
# NOT an exit: at the bound the discard re-emits the tag, zeroes the counter and
# keeps dropping, so the counter stays O(1) and a credential's continuation never
# resumes raw. Only a byte outside the arming anchor's value class ends the discard.
_STREAM_DISCARD_MAX = 1 << 20

# The withheld tail is a partial JWT/JWE when it ends with the `eyJ` base64url
# header prefix optionally followed by up to FOUR `.`-separated base64url segments
# (the final segment may be empty mid-stream). Three segments = a JWS/JWT
# (header.payload.sig); five = a compact JWE (header.key.iv.ciphertext.tag), so the
# `{0,4}` trailing quantifier admits the full JWE shape too — matching the batch
# `_CREDENTIAL_PATTERNS` JWE ceiling — instead of bisecting a >512-char JWE at the
# 512 floor. Anchored to the buffer end (`\Z`).
_PARTIAL_JWT_TAIL_RE = re.compile(
    rf"eyJ{_JWT_SEGMENT_VALUE_CLASS}+(?:\.{_JWT_SEGMENT_VALUE_CLASS}*){{0,4}}\Z"
)


def _partial_jwt_tail(buf: str) -> re.Match[str] | None:
    """The trailing partial JWT, skipping one whose finished header is not a JSON object."""
    m = _PARTIAL_JWT_TAIL_RE.search(buf)
    while m is not None and "." in m.group():
        header = m.group().split(".", 1)[0]
        if header.find("eyJ", 1) != -1 or redaction._is_json_object_segment(header):
            break
        m = _PARTIAL_JWT_TAIL_RE.search(buf, m.start() + 1)
    return m


# Trailing (possibly incomplete) `Authorization: Bearer <token>` anchor at the end
# of the stream buffer. Unlike a bare credential run, this anchor embeds WHITESPACE
# (`Authorization: Bearer `) which is NOT in `_CRED_CLASS`, so the maximal-trailing-
# cred-run holdback in `StreamRedactor.feed` would commit the `Authorization:` /
# `Bearer ` prefix in one chunk and the opaque token in the next — redacting
# neither, since the batch `Authorization:\s*Bearer` pattern only fires when the
# whole anchor is present in a single `redact()` call. We therefore withhold from
# the START of any such trailing anchor so the anchor and its token stay joined
# until a terminator (or stream end) arrives.
#
# `\Z` pins the match to the buffer tail so only a genuinely in-progress anchor is
# held. The `Bearer` word is matched by any of its prefixes (`B`…`Bearer`) so a
# split mid-word (`Authorization: Bear` | `er opaque…`) still holds; a completed
# anchor followed by a token then whitespace fails to match (`\s+` after the
# token cannot reach `\Z`), so it is committed and redacted whole. Requiring the
# `Bearer` prefix bounds over-holding: ordinary prose like `Authorization: granted`
# fails the match and is released immediately. Case-INSENSITIVE and JSON-aware to
# mirror the batch pattern: HTTP/2 lower-cases header names (`authorization:` /
# `bearer`) and JSON shapes the header as `{"Authorization": "Bearer <tok>"}` (a
# quote before the `:` and before the token), so the anchor tolerates an optional
# quote around `[:=]` and folds the `Authorization`/`Bearer` words — otherwise a
# lowercase or JSON-shaped anchor split across chunks would not be held and its
# token would leak. Opaque OAuth/refresh/SSO Bearer tokens carry no `eyJ` header,
# so without this anchor a >512-char opaque bearer tail would stay on the 512 floor
# and stream its raw tail.
_BEARER_ANCHOR_PARTIAL_RE = re.compile(
    r"""Authorization["']?\s*[:=]\s*["']?"""
    rf"(?:(?:Bearer|Basic|Token)(?:\s+{_BEARER_VALUE_CLASS}*)?"
    r"|Beare|Bear|Bea|Be|B|Basi|Bas|Ba|Toke|Tok|To|T)?\Z",
    re.IGNORECASE,
)


def _complete_token_match_crossing(
    matches: tuple[re.Match[str], ...], cut: int
) -> re.Match[str] | None:
    """Return the first token-parameter value strictly bisected by *cut*, if any."""
    for match in matches:
        # A cut at end(1) or end(1)+1 leaves the whole separator-name-equals-
        # value inside the commit, where the batch pass redacts it whole. Only a
        # cut inside the value strands an anchor-less suffix.
        if match.start() < cut < match.end(1):
            return match
    return None


def _aws_label_cut(buf: str, key_start: int) -> tuple[int, bool]:
    """Where an AWS label's safety cut goes, and whether its key is inside a batch match.

    A key name can be the tail of a longer secret-bearing token: a bare secret
    run that ends in `AccessKeyId`, a URL carrying an encoded key, or a value
    like `aws_session_token=...AccessKeyId`. The cut lands only at whitespace
    or at the start of the held buffer, and never inside a batch match, so
    every secret before the key reaches one batch call whole. The cost: a whitespace-free run that holds the key past the ceiling
    is dropped whole behind the fail-closed tag.
    """
    cut = _token_start(buf, key_start)
    inside = False
    if key_start > 0:
        spans = []
        for start, end in _batch_spans(buf, with_labels=False):
            if start >= key_start:
                break
            inside = inside or key_start < end
        # The cut never parts an earlier label from its value either: a closed
        # ``"password": "<value>"`` before the key reaches the window whole.
        for start, end in _batch_spans(buf, with_labels=True):
            if start >= key_start:
                break
            spans.append((start, end))
        # Latest start first, so a span that crosses an earlier move is seen too.
        # A match can start mid-token, so each move walks back to whitespace
        # again: a bare secret glued in front of the match stays whole.
        for start, end in sorted(spans, reverse=True):
            if start < cut < end:
                cut = _token_start(buf, start)
    return cut, inside


def _batch_spans(buf: str, *, with_labels: bool) -> list[tuple[int, int]]:
    """The batch pass's fixed-pattern matches and labelled values in *buf*, by start.

    A labelled value spans from its label's start when *with_labels* (a cut must
    not separate the two), else from the value's start (a key name inside a
    VALUE is part of that credential, not a label of its own).
    """
    redaction = _submodule("redaction")
    spans = [match.span() for match in redaction._credential_matches(buf)]
    for span in redaction.label_spans(buf):
        spans.append((span.context_start if with_labels else span.start, span.end))
    # A URI password with a ``/`` is read with its scheme and host around it:
    # a cut inside the URI parts the password from the context that finds it.
    for m in _submodule("redaction_labels")._URI_SLASH_PASSWORD_RE.finditer(buf):
        spans.append((m.start() if with_labels else m.start("value"), m.end()))
    return sorted(spans)


def _label_safe_cut(buf: str, cut: int) -> int:
    """*cut*, moved back to the start of any labelled value (label included) it
    falls inside, as the batch pass reads one on the text or its views."""
    labels = _submodule("redaction_labels")
    spans = [*_batch_spans(buf, with_labels=True), *labels.view_label_spans(buf)]
    for start, end in sorted(spans, reverse=True):
        if start < cut < end:
            cut = _token_start(buf, start)
    return cut


def _token_start(buf: str, pos: int) -> int:
    while pos > 0 and not buf[pos - 1].isspace():
        pos -= 1
    return pos


#: The longest word glued in front of a label (``aws_``, ``my-app.``).
_LABEL_PREFIX_MAX = 96


def _label_word_start(buf: str, pos: int) -> int:
    """Where the word that ends in the label at *pos* starts: back over name
    characters only, so compact JSON before it (a previous value, its quotes and
    commas) never counts toward the label's hold."""
    floor = max(0, pos - _LABEL_PREFIX_MAX)
    while pos > floor and (buf[pos - 1].isalnum() or buf[pos - 1] in "_-."):
        pos -= 1
    return pos


class StreamRedactor:
    """Rolling-buffer redactor for streamed LLM output.

    Feed raw chunks in order; ``feed`` returns the redacted, safe-to-broadcast
    prefix (possibly empty while a partial credential is buffered). Call
    ``flush`` when the stream/segment ends to redact and return the remainder.
    Adds at most one chunk of latency. A credential is never split across a
    commit boundary because commits only ever end at a non-credential-class
    character, while a credential is a contiguous credential-class run.
    """

    __slots__ = (
        "_buf",
        "_redact",
        "_discarding",
        "_discard_kind",
        "_discarded",
        "_last_commit",
        "_fed",
        "_encoded_checked",
        "_pending",
        "_long_token_from",
        "_committed_tail",
        "_catalog_keywords",
        "_out_line",
    )

    def __init__(self, redactor: "Callable[[str], str] | None" = None) -> None:
        self._buf = ""
        # Resolve at call time so module-load order is irrelevant.
        self._redact = redactor or redact
        self._discarding = False
        self._discard_kind: tuple[str, ...] | None = None
        self._discarded = 0
        # The last character committed, the characters fed so far, and how
        # many had been fed when a base64 run was last decoded
        # (``_encoded_secret_run``).
        self._last_commit = ""
        self._fed = 0
        self._encoded_checked = -1
        self._pending = 0
        self._long_token_from = -1
        # Every catalog keyword committed so far, and the last few committed
        # characters (a keyword the commit cut in two), so a vendor keyword
        # still selects its rules for a token that arrives in a later commit.
        self._committed_tail = ""
        self._catalog_keywords: frozenset[str] = frozenset()
        # The current line as emitted so far (``None`` once it is too long to
        # keep): a config line's start is judged on it.
        self._out_line: str | None = ""

    def _emitted(self, out: str) -> None:
        """Record *out* as emitted, for :attr:`_out_line`."""
        if "\n" in out:
            line: str | None = out[out.rindex("\n") + 1 :]
        else:
            line = None if self._out_line is None else self._out_line + out
        self._out_line = line if line is None or len(line) <= _OUT_LINE_MAX else None

    def _keyword_context(self) -> str:
        """The catalog keywords committed so far, then the committed tail."""
        return "\n".join((*sorted(self._catalog_keywords), self._committed_tail))

    def _redact_window(self, text: str) -> str:
        """Redact one context-incomplete window without URL-prefix exemptions."""
        redaction = _submodule("redaction")
        suspension = redaction._TOKEN_PARAM_EXEMPT_SUSPENDED.set(True)
        mid_line = credential_values.LINE_PREFIX.set(self._out_line)
        self._last_commit = text[-1:]
        try:
            out = self._redact(text)
        finally:
            credential_values.LINE_PREFIX.reset(mid_line)
            redaction._TOKEN_PARAM_EXEMPT_SUSPENDED.reset(suspension)
            seen = self._committed_tail + text
            self._catalog_keywords |= _submodule("secret_catalog").catalog_keywords_in(seen)
            self._committed_tail = seen[-_CATALOG_CONTEXT_TAIL:]
        if out:
            self._emitted(out)
        return out

    def feed(self, chunk: str) -> str:
        """Accept a chunk; return the redacted prefix that is safe to emit now."""
        if not chunk:
            return ""
        self._buf += chunk
        self._fed += len(chunk)
        # A delta that only lengthens a base64 run is gathered with the next
        # ones, so the passes below run once per few dozen such characters, not
        # per token-sized delta: on a long encoded blob each pass re-reads the
        # whole held buffer. Holding is always safe, it adds at most that many
        # characters to the buffer, and the next pass or ``flush`` sees them.
        # The same holds once the stream already holds a few hundred characters
        # (a label, a quoted value or an escaped payload waiting): every pass
        # re-reads all of them, and a little more holding is still safe.
        lengthens_run = self._buf[-len(chunk) - 1 : -len(chunk)] in _B64_RUN_CHARS and (
            _B64_RUN_CHARS.issuperset(chunk)
        )
        if (
            not self._discarding
            and (lengthens_run or len(self._buf) - len(chunk) >= _STREAM_COALESCE_HELD)
            and self._pending + len(chunk) < _STREAM_COALESCE
        ):
            self._pending += len(chunk)
            return ""
        self._pending = 0
        context = _submodule("secret_catalog").KEYWORD_CONTEXT.set(self._keyword_context())
        line = self._out_line
        mid_line = credential_values.LINE_PREFIX.set(line)
        try:
            out = self._feed_held(chunk)
        finally:
            credential_values.LINE_PREFIX.reset(mid_line)
            _submodule("secret_catalog").KEYWORD_CONTEXT.reset(context)
        # The windows of this pass recorded their text; the tags between them
        # are in *out* only, so the line is taken from the whole of it.
        self._out_line = line
        if out:
            self._emitted(out)
        return out

    def _feed_held(self, chunk: str) -> str:
        """One pass over the held buffer, *chunk* being its newest part."""
        # Owners of the re-exported names read below, resolved through the
        # import system so the value is read from the one place it lives.
        _redaction = _submodule("redaction")

        # Invariant: on entry while `_discarding`, `_buf` holds only the carried
        # tail of the already-tagged drop, which is never emitted unless a label
        # starts inside it; this chunk is the continuation of that drop.
        # Only a terminator byte exits the discard. Reaching the bound with no
        # terminator re-emits the tag and resets the counter but stays armed:
        # clearing the flag there would hand the credential's remaining bytes
        # to Phase A as an anchorless run, which the 512 floor streams raw.
        if self._discarding:
            assert self._discard_kind is not None
            run, kinds = stream_aws_anchor.discard_run(
                self._discard_kind, self._buf, _STREAM_DISCARD_RUN_RES
            )
            # A credential label glued to the dropped run (`.../Authorization:`,
            # `...SecretAccessKey = `) must survive it: its own value follows
            # whitespace the discard stops at, and would stream with no label.
            # Only when the AWS value is the sole carrier: an enclosing Bearer,
            # JWT or token value must keep dropping until its own terminator.
            if self._discard_kind == ("aws-keyed",):
                run = stream_aws_anchor.label_backoff(self._buf, run)
                # A key name the run ends at, followed so far only by a little
                # whitespace, may still get its separator in the next chunk:
                # keep it in the discard until that chunk decides.
                waiting = stream_aws_anchor.label_awaiting_separator(self._buf, run)
                if waiting is not None:
                    self._buf = self._buf[waiting:]
                    return ""
            # The carried tail was counted on the pass that first dropped it.
            self._discarded += max(0, run - (len(self._buf) - len(chunk)))
            # A quoted value's lone trailing backslash opens an escape whose
            # second half is still to come (``\\"`` inside the value, or the
            # escaped quote that closes it): keep it in the drop.
            tail = self._buf[run:]
            escape_open = (
                (
                    any(kind.startswith("quoted-") for kind in kinds)
                    and 0 < len(tail) <= 3
                    and tail == "\\" * len(tail)
                )
                or (
                    # A wrapped run's line break, and the first characters of its
                    # next line, may be cut by the chunk: keep them in the drop.
                    "encoded-run" in kinds
                    and _ENCODED_BREAK_TAIL_RE.fullmatch(tail) is not None
                )
                or (
                    # A single quote at the buffer end may be the first of a
                    # doubled one (``'it''s'``): the next chunk shows which.
                    "quoted-sq" in kinds
                    and tail == "'"
                )
                or (
                    # A block-scalar line cut while still only indentation: the
                    # next chunk shows whether it is body or the next key.
                    any(kind.startswith("block-") for kind in kinds)
                    and _BLOCK_PENDING_RE.fullmatch(tail) is not None
                )
            )
            pct = any(kind.startswith("quoted-pct") for kind in kinds)
            # The run took the encoded quote that closes the value (the body
            # never ends with one): the discard is over even at the buffer end.
            closed = pct and self._buf[:run].endswith(("%22", "%27"))
            if pct and not closed and run == len(self._buf):
                # An encoded quote cut by the chunk (``%`` or ``%2``) may be the
                # one that closes the value: keep it for the next pass.
                partial = len(self._buf) - max(self._buf.rfind("%"), 0)
                if self._buf.endswith(("%", "%2")) and partial <= 2:
                    tail = self._buf[len(self._buf) - partial :]
                    escape_open = True
            if escape_open:
                self._buf = tail
                return ""
            # A PEM drop is over once it took the block's ``-----END`` line.
            closed = closed or ("pem" in kinds and self._buf[:run].endswith("PRIVATE KEY-----"))
            if run == len(self._buf) and not closed:
                # Keep the tail still in the discard, so a label split across
                # chunks is seen whole on the next pass, and a PEM block's end
                # line cut by the chunk is read whole.
                carry = stream_aws_anchor.LABEL_CARRY if "aws-keyed" in kinds else 0
                if "pem" in kinds:
                    carry = _PEM_CARRY
                self._buf = self._buf[max(0, len(self._buf) - carry) :]
                self._discard_kind = kinds
                if self._discarded < _STREAM_DISCARD_MAX:
                    return ""
                self._discarded = 0
                return _redaction._REDACTED_CREDENTIAL_TAG
            self._discarding = False
            self._discard_kind = None
            self._buf = self._buf[run:]

        # PHASE A -- SAFETY CUT.
        # Invariant: every candidate can only move the cut backward. Bytes before
        # the minimum do not bisect any known in-progress credential anchor.
        natural_cut = _natural_cut(self._buf)
        if _submodule("redaction_labels").label_ends_near(self._buf, natural_cut):
            # A cut right after a label (``Authorization: Bearer <token>'``, or
            # one walked back through a terminal escape) would part the label
            # from its value: never place it inside a labelled value.
            natural_cut = _label_safe_cut(self._buf, natural_cut)
        elif "alue" in self._buf[max(0, natural_cut - 32) : natural_cut + 1]:
            # The ``value`` key of a ``name``/``value`` pair is no label of its
            # own: the pair's label is its sibling ``name`` field, which a cut
            # here would part from the value.
            natural_cut = _label_safe_cut(self._buf, natural_cut)
        if "://" in self._buf:
            # A URI password with a ``/`` and punctuation (``,`` ``;`` ``!``) is
            # outside the credential class: never cut inside its URI.
            for m in _submodule("redaction_labels")._URI_SLASH_PASSWORD_RE.finditer(self._buf):
                if m.start() < natural_cut < m.end():
                    natural_cut = _token_start(self._buf, m.start())
        partial_jwt = _partial_jwt_tail(self._buf)
        safety_cuts = [natural_cut]

        # Canonical credential tags are fixed points only when a batch-redaction
        # call sees the WHOLE tag. Their interior space is outside `_CRED_CLASS`,
        # so a chunk boundary inside a tag can otherwise commit its head and let
        # token-parameter pass 4 re-redact that fragment. Hold only a STRICT tag
        # prefix ending at the buffer tail. The nested search examines at most
        # the longest module-owned tag and only defers the cut: it is not a
        # credential anchor and therefore cannot escalate a cap or authorize a
        # fail-closed drop.
        partial_tag_start: int | None = None
        for tag in _redaction.CREDENTIAL_REDACTION_TAGS:
            max_prefix = min(len(self._buf), len(tag) - 1)
            for prefix_len in range(max_prefix, 0, -1):
                if self._buf.endswith(tag[:prefix_len]):
                    start = len(self._buf) - prefix_len
                    partial_tag_start = (
                        start if partial_tag_start is None else min(partial_tag_start, start)
                    )
                    break
        # PEM header hold-back (ported from the upstream project): the
        # multi-word phrase "BEGIN RSA PRIVATE KEY" splits on whitespace. If the
        # tail of the commit window contains an in-progress PEM header prefix,
        # refuse to commit at this boundary.
        pem_hold = natural_cut > 0 and bool(
            _PEM_HOLD_RE.search(self._buf[max(0, natural_cut - 50) : natural_cut])
        )

        # Bearer anchors are STRONG: their embedded whitespace is not in
        # _CRED_CLASS, so the natural cut could otherwise split anchor and value.
        bearer_anchor = _BEARER_ANCHOR_PARTIAL_RE.search(self._buf)
        if bearer_anchor is not None:
            safety_cuts.append(bearer_anchor.start())

        # AWS key-value labels are STRONG for the same reason: the label's
        # separator whitespace is outside _CRED_CLASS, and the value is only
        # redacted while its label reaches the same batch call.
        # Only a label that has reached its separator is STRONG. A bare key
        # name is WEAK: held only within the 512 floor, it never raises the cap
        # or arms a drop, so a key padded past the floor is released as on main.
        aws_label_seen = stream_aws_anchor.names_a_key(self._buf)
        aws_keyed_anchor = (
            stream_aws_anchor.find(self._buf, _CRED_CLASS, natural_cut) if aws_label_seen else None
        )
        if aws_keyed_anchor is not None:
            aws_cut, key_inside = _aws_label_cut(self._buf, aws_keyed_anchor.start())
            if key_inside:
                # A key name inside an earlier batch credential is part of that
                # credential's value, not a label: the batch pass never reads it
                # as one, so it anchors nothing. A real outer AWS label is the
                # leftmost anchor `find` returns, so it is not lost.
                aws_keyed_anchor = None
            else:
                if stream_aws_anchor.has_separator(aws_keyed_anchor):
                    safety_cuts.append(aws_cut)
                elif len(self._buf) - aws_cut <= _STREAM_HOLDBACK_MAX:
                    safety_cuts.append(aws_cut)
                    aws_keyed_anchor = None
                else:
                    # A bare label held past the floor would only move main's
                    # cut back into the run before it; release it as main does.
                    aws_keyed_anchor = None

        # The context rules need more than the value in one batch call: a key ID
        # beside a 40-character secret (either order), an AWS label written with
        # no separator, a ``name``/``value`` pair. Hold from where such a match
        # starts until its reach has arrived (``redaction_labels.stream_hold_start``).
        # WEAK: it never raises the cap or arms a drop, and the reach is under the
        # 512 floor.
        context_hold = _submodule("redaction_labels").stream_hold_start(self._buf)
        catalog_reach = False
        # Beside an AWS-keyed anchor the hold still applies within the floor: a
        # key ID before the label has its secret after it (``<ID> password=x
        # <secret>``), so the cut takes the earlier of the two.
        if context_hold is not None:
            hold_cut = _token_start(self._buf, context_hold)
            # Never hold from inside a labelled value: the cut would part the
            # value from its label. Move it back to where that match starts.
            # Nor from inside a key ID and its paired secret: the window that
            # holds the secret must hold its key ID too, or it is released bare.
            pairs: list[tuple[int, int]] = []
            _submodule("redaction_labels").paired_secret_spans(self._buf, pairs)
            # A vendor-catalog finding spans from its keyword, which the cut must
            # not part from its value either (``snowflake token: <value>\n``).
            catalog_spans = _submodule("secret_catalog").catalog_match_spans_near(
                self._buf, hold_cut
            )
            # A label the batch pass reads only through escapes or terminal codes
            # spans from that label too (``\x1b[33maws_secret_access_key\x1b[0m = ...``).
            view_spans = _submodule("redaction_labels").view_label_spans(self._buf)
            spans = [
                *_batch_spans(self._buf, with_labels=True),
                *pairs,
                *catalog_spans,
                *view_spans,
            ]
            for start, end in spans:
                if start < hold_cut < end:
                    hold_cut = _token_start(self._buf, start)
            # A hold past the floor would only move the floor's cut; a STRONG
            # anchor's cut already governs the buffer it is in. A vendor-catalog
            # value or an STS session token still arriving is the exception: it
            # is held as far as the long-token reach (``CATALOG_HOLD_REACH``).
            if len(self._buf) - hold_cut <= _STREAM_HOLDBACK_MAX:
                safety_cuts.append(hold_cut)
            elif (
                aws_keyed_anchor is None
                and len(self._buf) - hold_cut <= _submodule("redaction_labels").CATALOG_HOLD_REACH
                and _submodule("redaction_labels").long_token_start(self._buf) is not None
            ):
                safety_cuts.append(hold_cut)
                catalog_reach = True
                self._long_token_from = self._fed - len(self._buf) + hold_cut
        if not catalog_reach and self._long_token_from >= 0:
            # A vendor value or an STS session token held to its reach and
            # still arriving past it: fail closed. The text before it is
            # committed, the token goes as one tag and the rest of it is dropped
            # to its end, never released to the floor as an anchorless run.
            rel = self._long_token_from - (self._fed - len(self._buf))
            self._long_token_from = -1
            tail = self._buf[rel:] if 0 <= rel < len(self._buf) else ""
            blank = max((tail.rfind(c) for c in " \t\n\r"), default=-1)
            # A quoted value is the quoted-overrun path's (it drops to its quote).
            quoted = any(c in tail[:256] for c in "\"'\\")
            confirmed = (
                tail
                and not tail[-1].isspace()
                and blank <= 16
                and not quoted
                and any(
                    end == len(self._buf) and start >= rel
                    for start, end in (
                        *_batch_spans(self._buf, with_labels=False),
                        *(
                            (f_start, f_end)
                            for f_start, f_end, _rule in _submodule(
                                "secret_catalog"
                            ).catalog_findings(self._buf)
                        ),
                    )
                )
            )
            if confirmed:
                commit, self._buf = self._buf[:rel], ""
                self._discarding = True
                self._discard_kind = ("long-token",)
                self._discarded = 0
                out = self._redact_window(commit) if commit else ""
                return out + _redaction._REDACTED_CREDENTIAL_TAG
        # A labelled quoted value is held to its closing quote, as far as the
        # batch rule reads one: past the floor, but bounded (QUOTED_HOLD_REACH).
        labels = _submodule("redaction_labels")
        quoted_start = labels.open_quoted_start(self._buf)
        quoted_reach = False
        quoted_limit = labels.QUOTED_HOLD_REACH
        if quoted_start is not None and aws_keyed_anchor is None and bearer_anchor is None:
            quoted_cut = _label_word_start(self._buf, quoted_start)
            quoted_limit = labels.quoted_hold_reach(self._buf, quoted_start)
            if len(self._buf) - quoted_cut <= quoted_limit:
                safety_cuts.append(quoted_cut)
                quoted_reach = True

        # A labelled XML element whose value is still open (``<password>a;b``)
        # is held to its closing ``</`` like a quoted value, as far as the batch
        # rule reads one; past that reach the value goes as one tag and the rest
        # of it is dropped up to its closing tag, never released in pieces.
        xml_open = (
            labels.open_xml_value(self._buf)
            if quoted_start is None and aws_keyed_anchor is None and bearer_anchor is None
            else None
        )
        if xml_open is not None:
            xml_cut, xml_value = xml_open
            if len(self._buf) - xml_cut <= labels.QUOTED_VALUE_HOLD_REACH:
                safety_cuts.append(xml_cut)
                quoted_reach = True
                quoted_limit = labels.QUOTED_VALUE_HOLD_REACH
            else:
                commit, rest, self._buf = self._buf[:xml_value], self._buf[xml_value:], ""
                self._discarding = True
                self._discard_kind = ("xml-value",)
                self._discarded = 0
                out = self._redact_window(commit) if commit else ""
                return out + _redaction._REDACTED_CREDENTIAL_TAG + self.feed(rest)

        # A config line behind a secret label (``DB_PASSWORD=a,b;c``) is held to
        # its line end, as far as a quoted value; past that it fails closed: the
        # value goes as one tag and the rest of the line is dropped.
        config_open = labels.open_config_value(self._buf) if bearer_anchor is None else None
        if config_open is not None:
            config_cut = _label_word_start(self._buf, config_open[0])
            config_cut = self._buf.rfind("\n", 0, config_cut) + 1
            if len(self._buf) - config_cut <= labels.QUOTED_VALUE_HOLD_REACH:
                safety_cuts.append(config_cut)
                quoted_reach = True
                quoted_limit = labels.QUOTED_VALUE_HOLD_REACH
            else:
                value_at = config_open[1]
                commit, rest, self._buf = self._buf[:value_at], self._buf[value_at:], ""
                self._discarding = True
                self._discard_kind = ("quoted-line",)
                self._discarded = 0
                out = self._redact_window(commit) if commit else ""
                return out + _redaction._REDACTED_CREDENTIAL_TAG + self.feed(rest)

        # A PEM private key whose ``-----END`` line has not arrived is held from
        # its header, as far as an 8192-bit key runs; past that it fails closed:
        # the text before it is committed, the block goes as one tag and the
        # rest of it is dropped through its end line, as the batch pass reads it.
        pem_reach = False
        pem_open = None
        if "PRIVATE KEY-----" in self._buf:
            for header in _PEM_OPEN_RE.finditer(self._buf):
                pem_open = header
            pem_end = (
                _PEM_END_RE.search(self._buf, pem_open.end()) if pem_open is not None else None
            )
            # Closed only once something follows its end line: the end line at
            # the buffer end is still committed with its block.
            if pem_end is not None and pem_end.end() < len(self._buf):
                pem_open = None
        if pem_open is not None:
            if len(self._buf) - pem_open.start() <= _PEM_HOLD_REACH:
                safety_cuts.append(pem_open.start())
                pem_reach = True
            else:
                commit, rest, self._buf = (
                    self._buf[: pem_open.start()],
                    self._buf[pem_open.end() :],
                    "",
                )
                self._discarding = True
                self._discard_kind = ("pem",)
                self._discarded = 0
                out = self._redact_window(commit) if commit else ""
                return out + _redaction._REDACTED_CREDENTIAL_TAG + self.feed(rest)

        # A ``name``/``value`` pair whose unquoted value is still arriving is
        # held from its ``name`` field, as far as a quoted value; past that it
        # fails closed: the value goes as one tag and the rest is dropped.
        pair_open = (
            labels.open_name_value(self._buf)
            if not quoted_reach and aws_keyed_anchor is None and bearer_anchor is None
            else None
        )
        if pair_open is not None:
            pair_cut, pair_value = pair_open
            pair_cut = _token_start(self._buf, pair_cut)
            if len(self._buf) - pair_cut <= labels.QUOTED_VALUE_HOLD_REACH:
                safety_cuts.append(pair_cut)
                quoted_reach = True
                quoted_limit = labels.QUOTED_VALUE_HOLD_REACH
            else:
                commit, rest, self._buf = self._buf[:pair_value], self._buf[pair_value:], ""
                self._discarding = True
                self._discard_kind = ("view-unquoted",)
                self._discarded = 0
                out = self._redact_window(commit) if commit else ""
                return out + _redaction._REDACTED_CREDENTIAL_TAG + self.feed(rest)

        # A label opening a YAML block scalar (``password: |`` then indented
        # lines) is held with its body while the body may still be arriving,
        # as far as a quoted value: released at the floor, the body would
        # stream with no label left to redact it by.
        block_open = (
            labels.open_block_scalar(self._buf)
            if aws_keyed_anchor is None and bearer_anchor is None
            else None
        )
        if block_open is not None:
            block_cut = _label_word_start(self._buf, block_open)
            if len(self._buf) - block_cut <= labels.QUOTED_VALUE_HOLD_REACH:
                safety_cuts.append(block_cut)
                quoted_reach = True
                quoted_limit = labels.QUOTED_VALUE_HOLD_REACH
            else:
                # Past the reach (a long PEM key under ``key: |``): the label
                # and header are committed, the body goes as one tag and the
                # rest of it is dropped until a line returns to the label's
                # indent, never released line by line.
                # The label's indentation may already be committed: read the
                # line from the committed tail too.
                lead = len(self._committed_tail)
                header_end, indent = labels.block_scalar_header(
                    self._committed_tail + self._buf, lead + block_open
                )
                header_end -= lead
                commit, rest, self._buf = self._buf[:header_end], self._buf[header_end:], ""
                self._discarding = True
                self._discard_kind = (f"block-{min(indent, _BLOCK_INDENT_MAX)}",)
                self._discarded = 0
                out = self._redact_window(commit) if commit else ""
                return out + _redaction._REDACTED_CREDENTIAL_TAG + self.feed(rest)

        # A label padded towards its separator (``password`` then hundreds of
        # blanks) is held from its token, past the floor but bounded, since the
        # separator rule reads any run of blanks: released at the floor, the
        # label would stream before its value. It drops nothing, so a label that
        # never gets a separator is released unchanged.
        padded_reach = False
        padded = labels.padded_label_start(self._buf)
        if padded is not None:
            # From the token the label ends: a key name glued to the end of a
            # long value is part of that value, which is not cut inside.
            padded_cut = _token_start(self._buf, padded)
            if len(self._buf) - padded_cut <= _STREAM_HOLDBACK_JWT_MAX:
                safety_cuts.append(padded_cut)
                padded_reach = True

        # An unquoted value behind a label written encoded (``password%3D<v>``)
        # has no anchor on the text: held with its label like a quoted one, and
        # past that reach dropped to its end, so its tail never streams bare.
        if (
            len(self._buf) > _STREAM_HOLDBACK_MAX
            and not quoted_reach
            and aws_keyed_anchor is None
            and bearer_anchor is None
        ):
            # The whole held buffer: past the reach the label lies further back.
            view_open = labels.open_view_value(self._buf, len(self._buf))
            if view_open is not None:
                view_cut = _label_word_start(self._buf, view_open[0])
                if len(self._buf) - view_cut <= labels.QUOTED_VALUE_HOLD_REACH:
                    safety_cuts.append(view_cut)
                    quoted_reach = True
                    quoted_limit = labels.QUOTED_VALUE_HOLD_REACH
                else:
                    commit, rest, self._buf = (
                        self._buf[: view_open[1]],
                        self._buf[view_open[1] :],
                        "",
                    )
                    self._discarding = True
                    self._discard_kind = ("view-unquoted",)
                    self._discarded = 0
                    out = self._redact_window(commit) if commit else ""
                    return out + _redaction._REDACTED_CREDENTIAL_TAG + self.feed(rest)

        # A labelled quoted value still open past its hold reach is never
        # released: the text before it is committed, the value goes as one tag,
        # and the rest of it is dropped through its closing quote (fail closed),
        # so the stream matches the batch rule, which reads it whole.
        # Before the long-token cap can drop from the label's token start: a
        # label word glued in front (``aws_``) is at most 96 characters.
        overrun = labels.overrun_quoted_value(self._buf, _STREAM_HOLDBACK_JWT_MAX - 96)
        if overrun is not None and bearer_anchor is None:
            value_start, quote = overrun
            # The value so far goes through the discard itself, so an encoded
            # quote or escape the chunk cut in half is carried to the next pass.
            body = value_start + (len(quote) if quote.startswith("%") else 0)
            commit, rest, self._buf = self._buf[:value_start], self._buf[body:], ""
            self._discarding = True
            self._discard_kind = (_QUOTED_DISCARD_KIND.get(quote, "quoted-line"),)
            self._discarded = 0
            out = self._redact_window(commit) if commit else ""
            return out + _redaction._REDACTED_CREDENTIAL_TAG + self.feed(rest)

        # A token-name prefix without '=' is WEAK. It still needs a short
        # holdback so a chunk boundary cannot split the name, but it is not yet a
        # credential and must never escalate the cap or authorize data loss.
        token_anchor = _redaction._TOKEN_PARAM_PARTIAL_RE.search(self._buf)
        weak_token_anchor = None
        strong_token_anchor = False
        if token_anchor is not None:
            safety_cuts.append(token_anchor.start())
            strong_token_anchor = token_anchor.group("eq") is not None
            if not strong_token_anchor:
                weak_token_anchor = token_anchor

        # The PEM hold refuses any commit at this boundary.
        i = 0 if pem_hold else min(safety_cuts)
        complete_token_matches = tuple(_redaction._TOKEN_PARAM_RE.finditer(self._buf))
        # Invariant: the complete-match crossing predicate is re-evaluated after
        # every assignment to `i`; both Phase A and the Phase B floor call the
        # same helper rather than letting their predicate copies drift.
        complete_token_crossing = _complete_token_match_crossing(complete_token_matches, i)
        if complete_token_crossing is not None:
            i = min(i, complete_token_crossing.start())

        strong_anchored = (
            partial_jwt is not None
            or bearer_anchor is not None
            or aws_keyed_anchor is not None
            or complete_token_crossing is not None
            or strong_token_anchor
        )

        # A partial canonical tag is already-redacted material. It lowers only
        # the safety cut and is deliberately applied AFTER STRONG classification,
        # so the tag prefix itself can neither raise a cap nor authorize a drop.
        if partial_tag_start is not None:
            i = min(i, partial_tag_start)

        # PHASE B -- BOUNDS.
        # Invariant: STRONG anchors may fail closed instead of exposing a secret;
        # WEAK name prefixes never drop data. Any forced cut is repaired before
        # emission so it cannot strand an anchor-less token-value suffix.
        cap = _STREAM_HOLDBACK_MAX
        if quoted_reach and not strong_anchored:
            cap = max(cap, quoted_limit)
        if catalog_reach and not strong_anchored:
            cap = max(cap, labels.CATALOG_HOLD_REACH)
        if padded_reach and not strong_anchored:
            cap = max(cap, _STREAM_HOLDBACK_JWT_MAX)
        if pem_reach:
            cap = max(cap, _PEM_HOLD_REACH)
        if len(self._buf) - i > cap and strong_anchored:
            cap = _STREAM_HOLDBACK_JWT_MAX
        # A base64 run (or one wrapped over lines) whose decoded text holds a
        # secret is a credential anchor before this commit releases any of it:
        # the text before it is committed and the run goes as one tag, as the
        # batch pass replaces it whole; one still arriving is dropped to its end.
        if not strong_anchored:
            release = i if len(self._buf) - i <= cap else len(self._buf) - cap
            encoded = self._encoded_secret_run(release)
            if encoded is not None:
                start, end = encoded
                commit, rest, self._buf = self._buf[:start], self._buf[end:], ""
                out = self._redact_window(commit) if commit else ""
                if not rest:
                    self._discarding = True
                    self._discard_kind = ("encoded-run",)
                    self._discarded = 0
                    return out + _redaction._REDACTED_ENCODED_CREDENTIAL_TAG
                self._last_commit = ""
                return out + _redaction._REDACTED_ENCODED_CREDENTIAL_TAG + self.feed(rest)
        if len(self._buf) - i > cap:
            if strong_anchored:
                # Preserve the fail-closed ceiling for a real credential anchor:
                # redact the safe prefix, tag the event, and drop the oversized
                # tail rather than bisecting it and exposing the token head.
                # Only STRONG evidence arms the sticky discard. A WEAK trailing
                # name prefix (`&tok`, no `=`) is held by the safety cut but is
                # not yet a credential: it authorizes no drop and names no value
                # class to drop with.
                # Every carrier whose material reaches the buffer end arms its
                # own kind: a STRONG token anchor or a complete crossing ending
                # the buffer -> "token-param"; a Bearer anchor -> "bearer"; an
                # AWS value not closed yet -> "aws-keyed"; a partial JWT -> "jwt".
                # Another carrier can nest in an AWS value (a `?token=`), so with
                # an AWS carrier all of them stay armed and the discard ends only
                # where the last one does.
                token_param_reaches_end = strong_token_anchor or (
                    complete_token_crossing is not None
                    and complete_token_crossing.end(1) == len(self._buf)
                )
                aws_closed = aws_keyed_anchor is not None and (
                    aws_keyed_anchor.group("close") is not None
                )
                armed: list[str] = []
                if token_param_reaches_end:
                    armed.append("token-param")
                if bearer_anchor is not None:
                    armed.append("bearer")
                if aws_keyed_anchor is not None and not aws_closed:
                    armed.append("aws-keyed")
                if partial_jwt is not None:
                    armed.append("jwt")
                if "aws-keyed" not in armed:
                    # With no AWS carrier, the enclosing carrier alone defines
                    # where the credential ends: token-param, then bearer, then jwt.
                    armed = armed[:1]
                if armed == ["jwt"] and partial_jwt is not None and "." not in partial_jwt.group():
                    # A dotless ``eyJ`` run may be base64 of JSON, not a JWT
                    # segment: it runs on through ``+``, ``/`` and ``=`` too, so the
                    # drop lasts until whichever of the two ends later.
                    armed.append("encoded-run")
                if pem_hold and aws_keyed_anchor is not None and armed in ([], ["aws-keyed"]):
                    # The PEM hold only defers a commit at a whitespace boundary;
                    # it is not a credential. When an AWS value (open or closed by
                    # its quote) is the only thing failing closed, the drop starts
                    # at the AWS cut, not at 0,
                    # so a word like "begin" before the key cannot drop the
                    # benign text in front of it.
                    i = min(safety_cuts)
                    crossing = _complete_token_match_crossing(complete_token_matches, i)
                    if crossing is not None:
                        i = crossing.start()
                    if partial_tag_start is not None:
                        i = min(i, partial_tag_start)
                # A label glued inside the dropped AWS value (`.../SessionToken:`)
                # ends that drop: it stays buffered so its own value, after the
                # whitespace the discard would stop at, is held with it.
                # A label whose value has not started yet ends the drop at its own
                # key the same way, so only the run glued in front of it is dropped.
                glued_label = len(self._buf)
                if aws_keyed_anchor is not None and armed == ["aws-keyed"]:
                    if aws_keyed_anchor.group("open") or aws_keyed_anchor.group("val"):
                        glued_label = stream_aws_anchor.label_backoff(
                            self._buf, len(self._buf), aws_keyed_anchor.start("val")
                        )
                    elif aws_keyed_anchor.start() > i:
                        glued_label = aws_keyed_anchor.start()
                    if glued_label < len(self._buf):
                        armed = []
                credential_reaches_end = bool(armed)
                self._discarding = credential_reaches_end
                self._discard_kind = tuple(armed) if armed else None
                self._discarded = 0
                # A complete value may cross the safety cut yet end before a
                # benign suffix in the same buffer. Drop only through the value;
                # the suffix remains buffered for normal processing. Sticky
                # discard is reserved for credential material that reaches the
                # buffer end, where a continuation can still arrive. An AWS
                # value its quote has closed is such a complete value.
                drop_end = len(self._buf)
                if not credential_reaches_end:
                    ends = []
                    if complete_token_crossing is not None:
                        ends.append(complete_token_crossing.end(1))
                    if aws_closed:
                        assert aws_keyed_anchor is not None
                        ends.append(aws_keyed_anchor.start("close") + 1)
                    if glued_label < len(self._buf):
                        ends.append(glued_label)
                    drop_end = max(ends)
                if self._discard_kind == ("aws-keyed",):
                    # Carry the dropped tail into the discard, so a label name
                    # whose separator has not arrived yet is judged whole.
                    assert aws_keyed_anchor is not None
                    drop_end = max(
                        i,
                        aws_keyed_anchor.start("val"),
                        len(self._buf) - stream_aws_anchor.LABEL_CARRY,
                    )
                commit, self._buf = self._buf[:i], self._buf[drop_end:]
                out = self._redact_window(commit) if commit else ""
                return out + _redaction._REDACTED_CREDENTIAL_TAG
            i = len(self._buf) - cap
            i -= self._buf.startswith("${", i - 1)  # keep a JS/TS `${` placeholder whole

            # A labelled value still arriving past the floor (a long unquoted
            # ``value:`` beside its ``name:``): the floor would part it from its
            # label. The text before the value is committed, the value goes as
            # one tag and the rest of it is dropped to its end, as the batch
            # pass reads it.
            labelled = _batch_spans(self._buf, with_labels=True)
            values = _batch_spans(self._buf, with_labels=False)
            for l_start, l_end in labelled:
                if l_start < i and l_end == len(self._buf):
                    found = [v for v, e in values if e == l_end and v > l_start]
                    if found:
                        start = found[0]
                        commit, self._buf = self._buf[:start], ""
                        self._discarding = True
                        self._discard_kind = ("view-unquoted",)
                        self._discarded = 0
                        out = self._redact_window(commit) if commit else ""
                        return out + _redaction._REDACTED_CREDENTIAL_TAG

            # The floor was computed after Phase A, so re-check complete token
            # parameters against the actual cut. Advancing through the value is
            # safe because the whole parameter reaches one batch-redaction call,
            # and it shrinks the buffer rather than weakening the DoS bound.
            floor_crossing = _complete_token_match_crossing(complete_token_matches, i)
            if floor_crossing is not None:
                i = floor_crossing.end(1)
            # The same holds for a complete AWS key-value pair the floor would
            # split (a PEM hold can keep one buffered after its value ended):
            # its tail would stream with no label left to redact it.
            if aws_label_seen:
                for start, end in _batch_spans(self._buf, with_labels=True):
                    if start < i < end:
                        i = end
            # Any labelled value the floor would split, on the text or read
            # through an encoding (``password%3D%27<value>%27``), once it has
            # closed: the window takes it whole, or its tail streams label-less.
            for start, end in sorted(
                [
                    *_batch_spans(self._buf, with_labels=True),
                    *labels.view_label_spans(self._buf, labels.QUOTED_VALUE_HOLD_REACH),
                    # A vendor token the catalog finds whole (``fio-u-<64>``) has
                    # no label to redact its tail by once the floor parts it.
                    *(
                        (f_start, f_end)
                        for f_start, f_end, _rule in _submodule("secret_catalog").catalog_findings(
                            self._buf
                        )
                    ),
                ]
            ):
                if start < i < end < len(self._buf):
                    i = end

            # Repair the complete-match cut first, then preserve a trailing WEAK
            # prefix if that repair crossed it. An encoded separator is at most
            # 14 bytes and a name letter at most 42 (three `&#x0{0,8}HH;` slots),
            # so the clamp is <=224 bytes -- still under
            # `_STREAM_HOLDBACK_MAX = 512`.
            if weak_token_anchor is not None and weak_token_anchor.start() < i:
                i = weak_token_anchor.start()

        if i <= 0:
            return ""  # whole buffer is a (possibly partial) credential run — hold
        commit, self._buf = self._buf[:i], self._buf[i:]
        return self._redact_window(commit)

    def _encoded_secret_run(self, release: int) -> tuple[int, int] | None:
        """``(start, end)`` of a base64 run that decodes to a secret and that a
        commit up to *release* would part; ``end`` is the buffer end when the
        run is still arriving.

        The runs read are the one reaching the buffer end, the one continued
        from text already committed, and the one *release* falls in. A run is
        decoded again only when the commit would release characters fed since
        it was last decoded, so a long benign run costs one decode per held
        window, not one per chunk.
        """
        buf = self._buf
        if release <= 0:
            return None
        runs: list[tuple[int, int]] = []
        tail = _encoded_tail_start(buf)
        if tail is not None and tail < release:
            runs.append((tail, len(buf)))
        if (
            self._last_commit
            and self._last_commit in _B64_LINE_CHARS
            and buf[:1] in _B64_LINE_CHARS
        ):
            runs.append((0, _encoded_head_end(buf)))
        if release < len(buf) and buf[release - 1] in _B64_LINE_CHARS:
            end = release + _encoded_head_end(buf[release:])
            start = _encoded_tail_start(buf[:end])
            if start is not None and start < release:
                runs.append((start, end))
        if not runs:
            return None
        released_upto = self._fed - len(buf) + release
        if released_upto <= self._encoded_checked:
            return None
        self._encoded_checked = self._fed
        for start, end in runs:
            if end - start >= 40 and _decodes_to_secret(buf[start:end]):
                return start, end
        return None

    def flush(self) -> str:
        """Redact and return the buffered remainder; clears the buffer."""
        if self._discarding:
            self._buf = ""
            self._discarding = False
            self._discard_kind = None
            return ""
        context = _submodule("secret_catalog").KEYWORD_CONTEXT.set(self._keyword_context())
        try:
            out = self._redact_window(self._buf) if self._buf else ""
        finally:
            _submodule("secret_catalog").KEYWORD_CONTEXT.reset(context)
        self._buf = ""
        return out

    @property
    def held(self) -> str:
        """The raw run withheld so far (``""`` while a value is being dropped)."""
        return "" if self._discarding else self._buf

    @property
    def discarding(self) -> bool:
        """True while the rest of a value is being dropped."""
        return self._discarding

    def reset(self) -> None:
        """Discard the buffer without emitting (segment abandoned/cleared)."""
        self._buf = ""
        self._committed_tail = ""
        self._catalog_keywords = frozenset()
        self._out_line = ""
        self._discarding = False
        self._discard_kind = None
        self._discarded = 0


def redact_and_truncate(text: str, max_chars: int = 4000) -> str:
    """Redact credentials and exfiltration URLs, then truncate.

    Redaction runs over the full text BEFORE the ``max_chars`` slice so a
    credential (or base64/URL blob) straddling the truncation boundary cannot
    leak as an unredacted partial fragment. Truncating first
    would cut a secret in half, leaving a prefix that fails to match the
    credential regex and therefore escapes redaction.
    """
    return redact_with_findings(text or "")[0][:max_chars]

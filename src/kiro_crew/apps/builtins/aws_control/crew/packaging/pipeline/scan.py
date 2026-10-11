"""Credential scanning -- refuse, never warn.

Ported in INTENT from ``crew_export/scan.py``, which delegates to
``kiro_crew.deploy.scan`` for the canonical pattern set. That module is NOT
importable in this venv, so the hard-credential patterns below are a
self-contained subset. This is a real narrowing versus the source and is
called out in the track report: a credential shape the canonical set knows and
this subset does not would pass. The credential-NAME gate is ported verbatim.

``scan_text`` is the one scanner for the text a bundle SHIPS: the prompt, each skill file,
each MCP server definition and the rendered ``agent.json``, and every staged leaf once more
as it is written. A finding carries four characters of the match and its length, never the
matched bytes, so a refusal that quotes a finding does not print the secret it found.
"""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The AWS key-ID prefix group is taken from ``kiro_crew.credential_patterns`` when
# that import works, because a second hand-written copy of it is exactly the drift a
# repo guard exists to catch (``test_no_module_spells_the_prefix_group_by_hand``).
# The literal fallback keeps this module runnable standalone, which is the property
# that lets it be exercised as ``python -m packaging.build`` from the crew directory
# alone -- so the fallback is the exception, not the normal path.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.credential_patterns import AWS_KEY_ID_PREFIXES as _AWS_KEY_PREFIXES
except Exception:  # pragma: no cover
    _AWS_KEY_PREFIXES = "AKIA|ASIA"

# The vendor and forge token spellings are imported from the shared module so this
# subset cannot drift from the scrubber: a format added there reaches here with no
# edit, and no one-sided omission can hide. The fallback restates the same shapes
# for the standalone case where ``kiro_crew`` is not importable at all -- with the
# hyphen INSIDE the ``sk-proj-`` / ``sk-ant-`` classes and a length-flexible
# ``github_pat_``, the two spellings whose drifted forms had leaked.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.credential_patterns import VENDOR_TOKEN_PATTERNS as _VENDOR_TOKEN_PATTERNS
except Exception:  # pragma: no cover
    _VENDOR_TOKEN_PATTERNS = (
        ("openai-project-key", r"sk-proj-[A-Za-z0-9_-]{16,}"),
        ("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{16,}"),
        ("vendor-key", r"sk-[A-Za-z0-9]{20,}"),
        ("github-fine-grained-pat", r"github_pat_[A-Za-z0-9_]{40,}"),
        ("gitlab-pat", r"glpat-[A-Za-z0-9_-]{16,}"),
        ("npm-token", r"npm_[A-Za-z0-9]{24,}"),
        ("pypi-token", r"pypi-[A-Za-z0-9_-]{16,}"),
    )

#: The vendor/token fragments compiled with word boundaries for the standalone scan.
_VENDOR_TOKEN_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(rf"\b{fragment}\b")) for label, fragment in _VENDOR_TOKEN_PATTERNS
)

# The redactor keeps the key that names a value and replaces the value alone, so a
# skill stored already redacted reads ``aws_secret_access_key=[REDACTED: credential]``
# -- and a labelled pattern that ran on every line, whether or not the canonical
# detector loads, matched its ``[REDACTED:`` head as the value and aborted the crew
# build on text that holds no secret. The value is therefore read by a VENDORED copy
# of the canonical value scanner (``kiro_crew.security.scan_keyed_value``): one
# tokenizer, one token per step, with the opener, the escaped-whitespace head, the
# tag run, the escape pair, the doubled quote and the line end as its rules, so the
# standalone path cannot drift from the canonical one on a quote or escape shape
# (``test/test_redaction_keyed_value_fixture.py`` pins the two to one generated
# fixture). A tag run is exempt only where it FILLS the value; a tag heading a
# quoted value that continues, glued bytes, a tag closed by a doubled quote, or a
# tag in another case is a value and still a finding. The registry of the
# redactor's own tags is read from the scrubber so a tag added there reaches here
# with no edit; the fallback restates the two literals for the standalone case.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.security import CREDENTIAL_REDACTION_TAGS as _REDACTION_TAGS
except Exception:  # pragma: no cover
    _REDACTION_TAGS = ("[REDACTED: credential]", "[REDACTED: encoded credential]")

_LABEL_RE = re.compile(
    r"(?:SecretAccessKey|aws_secret_access_key|SessionToken|aws_session_token)"
    r"(?:\\?[\"'])?\s*[:=]\s*",
    re.IGNORECASE,
)
_WS_ESCAPES = frozenset("nrtfv")
_QUOTES = frozenset("\"'")
#: The canonical scanner's line-quote state: (open literal kind, backslash
#: pending, kind a quote just closed, last byte, inner literal's escaped
#: delimiter); a line starts outside.
_LINE_START = ("", False, "", "", "")


def _advance_line_state(state, text: str, start: int, end: int):
    """Vendored ``_advance_line_state``: a raw line break resets; outside a
    literal a quote opens one unless the byte before it is a word byte, and a
    backslash escapes the next byte; inside, the same kind closes, a doubled
    quote reopens as an escaped interior quote, and an escaped quote opens or
    closes the inner literal of the escaped encoding."""
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


def _enclosing_at(text: str, at: int) -> str:
    """Vendored ``_enclosing_at``: the literals enclosing *at* (outer bare quote,
    then the inner escaped delimiter), read back along the line."""
    line_start = max(text.rfind("\n", 0, at), text.rfind("\r", 0, at)) + 1
    state = _advance_line_state(_LINE_START, text, line_start, at)
    return state[0] + state[4]


def _innermost(enclosing: str) -> tuple[str, str]:
    if len(enclosing) >= 3 and enclosing[-2] == "\\":
        return enclosing[-2:], enclosing[:-2]
    return enclosing[-1:], ""


def _tag_run_end(text: str, i: int) -> int:
    while True:
        for tag in _REDACTION_TAGS:
            if text.startswith(tag, i):
                i += len(tag)
                break
        else:
            return i


def _inner_token(
    text: str, i: int, escaped: bool, literal_quote: str, enclosing: str = ""
) -> tuple[str, int]:
    c = text[i]
    if c == "\\":
        if i + 1 >= len(text):
            return "partial", 1
        if escaped or enclosing.startswith('"') or len(enclosing) >= 3:
            # The pair is ONE token, read before any delimiter test: inside a
            # backslash-escaping literal (a `"` literal or an escaped inner one)
            # every byte is in the literal's encoding whatever quote opens the
            # value; a bare `'` literal has no escapes of its own.
            nxt = text[i + 1]
            if nxt in "\r\n":
                return "backslash", 1  # no encoding pairs a backslash with a raw line break
            if nxt == "\\":
                return "backslash", 2
            if nxt in "\"'":
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
    if c in "\"'":
        return "quote", 1
    return "char", 1


def _prefix_run_end(text: str, i: int, enclosing: str) -> int:
    """The index past the PREFIX run the structural byte (`,`, `}`, `]`) at *i*
    heads: further structural bytes, a backslash paired with a structural byte or
    another backslash (the escaped encoding's inner `\\\\` among them), and the
    enclosing encoding's escaped whitespace between them. Read whole before the
    value is judged: the base's value class admits `]` and a backslash, so
    `key=]]<secret>` is a value to it, and a judgement of the first byte alone
    left the secret after the pair standing. A backslash pair never heads a run:
    there it is the value's first byte, so the caller asks only at a structural
    byte."""
    n = len(text)
    while True:
        j = i
        while j < n:
            kind, width = _inner_token(text, j, False, "", enclosing)
            if kind == "char" and width == 1 and text[j] in ",}]":
                j += 1
            elif kind == "backslash" and (width == 2 or text[j + 1] in ",}]\\"):
                j += 2
            else:
                break
        while j < n:  # the enclosing encoding's escaped whitespace after the run
            kind, width = _inner_token(text, j, False, "", enclosing)
            if kind in ("space", "break") and width == 2:
                j += width
                continue
            if kind != "backslash" or j + width >= n:
                break
            letter_kind, letter_width = _inner_token(text, j + width, False, "", enclosing)
            if letter_kind != "char" or text[j + width] not in _WS_ESCAPES:
                break
            j += width + letter_width
        if j == i:
            return i
        i = j


def _no_value_opens_at(text: str, i: int, enclosing: str) -> bool:
    """Whether NO value opens at the structural byte at *i*, where an unquoted
    value would start: past the prefix run it heads nothing value-like follows --
    whitespace, a line break, a quote (bare or escaped), a close, or a bare
    backslash before one of those -- read as the inner token in the pair's
    encoding; a value byte after the run makes the run the value's head. A run
    reaching the text's end is no value, decided by the caller before this is
    asked."""
    j = _prefix_run_end(text, i, enclosing)
    kind, _width = _inner_token(text, j, False, "", enclosing)
    if kind == "backslash":
        return text[j + 1] in "\"'" or text[j + 1].isspace()
    return kind != "char"


def _scan_value(text: str, at: int, enclosing: str | None = None) -> tuple[int, int, bool, str]:
    """Vendored ``scan_keyed_value``: ``(start, end, closes, opener)`` of the value
    whose separator ends at *at*, inside the literal *enclosing* (read back along
    the line when not given). Byte-for-byte the canonical scanner's claim."""
    if enclosing is None:
        enclosing = _enclosing_at(text, at)
    n = len(text)
    opener = ""
    if at < n and text[at] in "\"'":
        opener = text[at]
    elif at + 1 < n and text[at] == "\\" and text[at + 1] in "\"'":
        opener = text[at : at + 2]
    innermost, outer = _innermost(enclosing)
    if enclosing and opener == enclosing[0] and text[at + 1 : at + 2] == opener:
        # A doubled enclosing-kind quote where the value would open is the
        # literal's escaped interior quote: the value's own quote, as written.
        opener = opener * 2
    elif enclosing and opener in (innermost, enclosing[0]):
        # The enclosing literal's own close (the inner literal's escaped
        # delimiter, or the outer literal's bare quote, which closes everything):
        # the assignment inside it is empty and what follows the close is in the
        # context outside it.
        return _scan_value(text, at + len(opener), outer if opener == innermost else "")
    escaped = opener.startswith("\\")
    literal_quote = opener[-1] if opener else ""
    start = i = at + len(opener)
    while i < n:  # the head: escaped whitespace, unbounded
        kind, width = _inner_token(text, i, escaped, literal_quote, enclosing)
        if kind in ("space", "break") and width == 2:
            i += width  # the enclosing encoding's escaped whitespace, a line break among it, is whitespace to the anchor
            continue
        if kind != "backslash" or i + width >= n:
            break
        letter_kind, letter_width = _inner_token(text, i + width, escaped, literal_quote, enclosing)
        if letter_kind != "char" or text[i + width] not in _WS_ESCAPES:
            break
        i += width + letter_width
    if not opener:
        while i < n:
            run = _tag_run_end(text, i)
            if run > i:
                i = run
                continue
            kind, width = _inner_token(text, i, False, "", enclosing)
            if kind == "partial":
                return start, i, True, ""
            if kind == "char" and i == start and text[i] in ",}]":
                # The token past the PREFIX run decides: the text's end,
                # whitespace, a quote or a close, and no value opens; a value
                # byte, and the run is the value's head, consumed with it.
                run = _prefix_run_end(text, i, enclosing)
                if run >= n or (text[run] == "\\" and run + 1 >= n):
                    return start, i, True, ""
                if _no_value_opens_at(text, i, enclosing):
                    return start, i, True, ""
                i = run
                continue
            if kind in ("space", "break", "close", "quote") or (
                kind == "char" and i > start and text[i] in ",}"
            ):
                return start, i, True, ""
            if kind == "backslash":
                # A pair token is a byte of the value; a BARE backslash reads the
                # byte after it (a lone one at the text's end is `partial` above):
                # a quote or raw whitespace ends the value, any other pair is the
                # value's, the two-byte spelling `\n` of a decoded URL path among them.
                nxt = text[i + 1]
                if width == 1 and (nxt in "\"'" or nxt.isspace()):
                    return start, i, True, ""
                i += 2
                continue
            i += width
        return start, n, True, ""
    while i < n:
        run = _tag_run_end(text, i)
        if run > i:
            i = run
            continue
        kind, width = _inner_token(text, i, escaped, literal_quote, enclosing)
        if kind == "close" and width == 1 and text[i + 1 : i + 2] == text[i]:
            # A doubled enclosing-kind quote is an escaped interior quote; when
            # the value opened with it, doubled again it is the value's escaped
            # interior quote and alone it is the value's close.
            if opener == text[i] * 2:
                if text[i + 2 : i + 4] == opener:
                    i += 4
                    continue
                return start, i, True, opener
            i += 2
            continue
        if kind in ("partial", "break", "close"):
            return start, i, False, opener
        if kind == "backslash":
            j = i + width
            if j >= n:
                return start, i, False, opener
            nxt_kind, nxt_width = _inner_token(text, j, escaped, literal_quote, enclosing)
            if nxt_kind == "break":
                i = j  # a backslash does not escape a line break, raw or inner
                continue
            if nxt_kind == "partial":
                return start, j, False, opener
            if nxt_kind == "close" and nxt_width == 1:  # a bare enclosing close is never escaped
                # Escapes pair up at the value's own depth: after the escaped
                # encoding's inner backslash (`\\`) a BARE quote is the enclosing
                # literal's close, never the escaped token; doubled, its escaped
                # interior quote.
                if text[j + 1 : j + 2] == text[j]:
                    i = j + 2
                    continue
                return start, j, False, opener
            i = j + nxt_width
            continue
        if kind == "quote" and text[i : i + width] == opener:
            j = i + width
            if j < n and text[j : j + width] == opener:
                i = j + width
                continue
            return start, i, True, opener
        i += width  # a quote of the other kind is a byte of the value
    return start, n, False, opener


def _is_tag_run(text: str, start: int, end: int) -> bool:
    return end > start and _tag_run_end(text, start) == end


class _LabelledSecretMatch:
    """The ``re.Match`` surface the two consumers below read: the matched text."""

    def __init__(self, text: str, start: int, end: int) -> None:
        self._text, self._start, self._end = text, start, end

    def group(self, _index: int = 0) -> str:
        return self._text[self._start : self._end]

    def start(self) -> int:
        return self._start


class _LabelledSecretMatcher:
    """A LABELLED secret: the key naming an AWS secret or session token, its
    separator, and a LIVE value as the vendored scanner reads it -- a non-empty
    value that is not a registered tag run filling it. ``search`` returns the
    first such pair on the line, as a ``re.Pattern`` would."""

    def search(self, text: str) -> _LabelledSecretMatch | None:
        state, pos = _LINE_START, 0
        for label in _LABEL_RE.finditer(text):
            state = _advance_line_state(state, text, pos, label.end())
            pos = label.end()
            start, end, closes, _opener = _scan_value(text, label.end(), state[0] + state[4])
            if end <= start:
                continue
            if _is_tag_run(text, start, end) and closes:
                continue
            return _LabelledSecretMatch(text, label.start(), end)
        return None


_HARD_PATTERNS: tuple[tuple[str, Any], ...] = (
    ("aws-access-key", re.compile(rf"\b(?:{_AWS_KEY_PREFIXES})[0-9A-Z]{{16}}\b")),
    # A LABELLED secret. The pattern above matches an AWS key ID, which has a
    # recognisable prefix; the secret access key is 40 characters of base64 with no
    # prefix at all, so nothing above can see it and `SecretAccessKey=<secret>` in a
    # prompt reached the deployed image. What makes it findable is the label, which is
    # how this repo's own detector finds it (``security.exfil.hard_credential_hit``,
    # described in security_posture.py as covering "labelled secret-access-key and
    # session-token forms"). The value is the vendored scanner's, so the two agree
    # on every quote and escape shape; the canonical module is preferred below when
    # importable.
    ("aws-secret-labelled", _LabelledSecretMatcher()),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    # The same header after URL or form encoding, where the spaces have become ``+`` or
    # ``%20``. The shared detector spells its separator ``[\s+%]`` for exactly this, and
    # copying it as a literal space here left the encoded form unmatched -- measured against
    # ``_HARD_CREDENTIAL_RE``, ``BEGIN+RSA+PRIVATE+KEY`` was caught there and missed here.
    # A persona pasted out of a browser or a curl transcript arrives in that shape.
    (
        "private-key-encoded",
        re.compile(r"BEGIN[\s+%]+(?:RSA|DSA|EC|OPENSSH)[\s+%]+PRIVATE[\s+%]+KEY"),
    ),
    # An SSH PUBLIC key line. Not itself a secret, and that is not the test this scan
    # applies: the shared detector refuses these too, because a key line in a bundled
    # persona means a keypair was pasted in and the private half is very likely beside it.
    # Missing from the local subset until a comparison against the shared patterns was run
    # rather than eyeballed.
    ("ssh-public-key", re.compile(r"\b(?:ssh-rsa|ssh-ed25519)[\s+%]")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Vendor and forge API tokens (OpenAI project/vendor, Anthropic, fine-grained
    # GitHub PAT, GitLab PAT, npm, PyPI) sourced from the shared module above so the
    # standalone subset stays in lockstep with the scrubber. The fine-grained PAT and
    # the ``sk-proj-`` / ``sk-ant-`` forms are the shapes whose hand-restated spellings
    # here had drifted and shipped credentials unscanned in the deployment venv.
    *_VENDOR_TOKEN_COMPILED,
    # A JWT (three base64url segments split by dots, header starting ``eyJ``). Bearer tokens,
    # session tokens and signed credentials arrive in this shape pasted into a persona, and
    # the local set had no way to see one. The header segment is anchored on ``eyJ`` (``{"``
    # base64url-encoded) so an ordinary dotted identifier is not matched.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)


@dataclass(frozen=True)
class Leak:
    origin: str
    kind: str
    line: int
    snippet: str

    def render(self) -> str:
        return f"{self.origin}:{self.line}: {self.kind}: {self.snippet}"


#: The repository's own hard-credential detector, when this module can reach it. The
#: local ``_HARD_PATTERNS`` above is a self-contained SUBSET and was documented as a
#: real narrowing; a review then found the exact gap that narrowing left (a labelled
#: AWS secret access key). So prefer the canonical one and keep the subset as the
#: fallback that lets this module run without ``kiro_crew`` installed -- the same
#: bargain ``_AWS_KEY_PREFIXES`` strikes, for the same reason.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.security import hard_credential_hit

    _CANONICAL_CREDENTIAL_HIT: Callable[[str], bool] | None = hard_credential_hit
except Exception:  # pragma: no cover
    _CANONICAL_CREDENTIAL_HIT = None

#: The repo's redactor, imported for its ENCODED-credential detection. The patterns above
#: all match a credential written literally, so a base64 of the same bytes matched none of
#: them. This one decodes base64 chunks, and its warning list is what ``scan_text`` reads;
#: the redacted text is discarded, because this module refuses rather than edits.
#:
#: Imported rather than restated for the reason the canonical pattern is: a local subset
#: needs a new entry per shape, which does not converge.
try:  # pragma: no cover - exercised by whichever branch the environment allows
    from kiro_crew.security import redact_credentials

    _CANONICAL_REDACTOR: Callable[[str], tuple[str, list[str]]] | None = redact_credentials
except Exception:  # pragma: no cover
    _CANONICAL_REDACTOR = None


_BARE_SECRET_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}(?![A-Za-z0-9+/])")
_BARE_SECRET_LEN = 40
_BARE_SECRET_ENTROPY_MIN = 4.3
_BARE_SECRET_MAX_LOWER_RUN = 5
_BARE_SECRET_MAX_VOWEL_RATIO = 0.30
_BARE_SECRET_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")
_BARE_SECRET_VOWELS = frozenset("aeiouAEIOU")


def _bare_secret_decodes_to_printable(token: str) -> bool:
    """A base64 run whose decode is printable text is an encoded blob, not a bare key."""
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except Exception:
        return False
    if not raw:
        return False
    printable = sum(1 for b in raw if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D))
    return printable / len(raw) >= 0.85


def _bare_secret_window_is_key(token: str) -> bool:
    """One 40-char window has the shape of a bare AWS secret access key.

    A faithful, self-contained mirror of the canonical structural classifier, so the
    standalone deployment-path scan is not strictly weaker than the canonical one for this
    known shape. Every gate must pass, and the bias is toward NOT flagging: a false negative
    reverts to prior behaviour, a false positive refuses a benign build. Gates: exactly 40
    chars; all three of lower + upper + digit (rejects prose and all-one-class runs); not
    hex-only (a git sha or hex digest); no lowercase run over the cap (rejects dictionary-word
    identifiers and path segments); vowel ratio at or under the cap; Shannon entropy at or
    above the floor; and it does not base64-decode to printable text (an encoded blob is the
    decode pass's job, not this one).
    """
    if len(token) != _BARE_SECRET_LEN:
        return False
    if not (
        any(c.islower() for c in token)
        and any(c.isupper() for c in token)
        and any(c.isdigit() for c in token)
    ):
        return False
    if _BARE_SECRET_HEX_ONLY_RE.match(token):
        return False
    run = 0
    for ch in token:
        run = run + 1 if ch.islower() else 0
        if run > _BARE_SECRET_MAX_LOWER_RUN:
            return False
    letters = [ch for ch in token if ch.isalpha()]
    if letters and (
        sum(1 for ch in letters if ch in _BARE_SECRET_VOWELS) / len(letters)
        > _BARE_SECRET_MAX_VOWEL_RATIO
    ):
        return False
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = -sum((c / len(token)) * math.log2(c / len(token)) for c in counts.values())
    if entropy < _BARE_SECRET_ENTROPY_MIN:
        return False
    return not _bare_secret_decodes_to_printable(token)


def _scan_bare_secret_runs(text: str, origin: str) -> list[Leak]:
    """Findings for a bare, unlabelled AWS secret access key in *text*.

    The canonical redactor catches this by shape in its bare-secret pass; the standalone path
    has only labelled patterns and a decode pass, and a bare 40-char secret carries no label
    and decodes to non-UTF-8 bytes, so without this it ships. A structural DETECTOR rather than
    a fourth literal pattern -- the shape that converges. A genuine 40-char key glued to
    adjacent base64 characters yields a 41+ char run, so a 40-char window is slid across each
    run (disjoint spans keep it linear); a run that decodes whole to printable text is a
    cohesive encoded blob and is left to the decode pass.
    """
    found: list[Leak] = []
    for match in _BARE_SECRET_RUN_RE.finditer(text):
        run = match.group(0)
        if _bare_secret_decodes_to_printable(run):
            continue
        for start in range(0, len(run) - _BARE_SECRET_LEN + 1):
            window = run[start : start + _BARE_SECRET_LEN]
            if _bare_secret_window_is_key(window):
                found.append(
                    Leak(
                        origin=origin,
                        kind="bare-secret",
                        line=0,
                        snippet=window[:4] + "…(%d chars)" % len(window),
                    )
                )
                break
    return found


def scan_text(text: str, origin: str) -> list[Leak]:
    """Hard credential findings in *text*. A finding aborts the build."""
    leaks: list[Leak] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _HARD_PATTERNS:
            m = pattern.search(line)
            if m:
                token = m.group(0)
                snippet = token[:4] + "…(%d chars)" % len(token)
                leaks.append(Leak(origin=origin, kind=kind, line=lineno, snippet=snippet))
        if _CANONICAL_CREDENTIAL_HIT is not None and _CANONICAL_CREDENTIAL_HIT(line):
            leaks.append(
                Leak(
                    origin=origin,
                    kind="repo-credential-detector",
                    line=lineno,
                    snippet=line.lstrip()[:4] + "…(%d chars)" % len(line),
                )
            )
    # Encoded credentials, via the repo's OWN redactor rather than a fourth local pattern.
    #
    # ``_HARD_PATTERNS`` and the canonical detector both match a credential written
    # literally. A base64 of the same bytes matches neither, so a labelled secret survived
    # every scan and shipped -- and this module already knows that adding one more local
    # pattern per shape is what does not converge, which is why the prompt fence prefers
    # ``is_sensitive_path`` over its own list.
    #
    # ``redact_credentials`` decodes base64 chunks and reports what it found, so its WARNING
    # list is the signal here; the redacted text is discarded because this function refuses
    # rather than edits. Run over the whole text, not per line: an encoded blob can wrap.
    if _CANONICAL_REDACTOR is not None:
        try:
            _, warnings = _CANONICAL_REDACTOR(text)
        except Exception:  # a detector fault must not become a silent pass
            warnings = ["credential redactor raised; treating the content as unscannable"]
        for warning in warnings:
            leaks.append(Leak(origin=origin, kind="repo-redactor", line=0, snippet=warning[:80]))
    else:
        # The import failed, which is the documented standalone mode. Encoded detection must
        # not simply VANISH with it: a build that silently stops looking for a class of leak
        # is worse than one that never claimed to, because the plan's notes still say the
        # content was scanned.
        #
        # So the fallback DECODES rather than re-describing what a credential looks like. It
        # feeds ``_HARD_PATTERNS`` -- the same patterns the literal pass uses -- over the
        # decoded bytes. That is deliberately not a fourth local credential pattern: adding
        # one pattern per shape is the shape that does not converge, and a decoder
        # inherits every future pattern for free where a pattern list would not.
        leaks.extend(_scan_decoded_runs(text, origin))
        # The canonical redactor's bare-secret pass has no counterpart in the patterns above,
        # so a label-less 40-char AWS secret access key -- which matches no ``_HARD_PATTERNS``
        # entry and base64-decodes to non-UTF-8 bytes the decode pass skips -- would ship only
        # in this standalone mode. The structural detector closes that so the deployment-path
        # scan is not weaker than the canonical one for this shape.
        leaks.extend(_scan_bare_secret_runs(text, origin))
    return leaks


#: Base64 runs long enough to hide a credential. The floor is 20 characters, not 40: 40 is
#: the length of an AWS *secret access key* specifically, but ``_HARD_PATTERNS`` also matches
#: shorter secrets (a labelled ``aws_secret_access_key=<value>`` fragment, a vendor ``sk-``
#: key, a github/slack token) whose base64 run is well under 40 chars, and in the standalone
#: deployment venv this decoder is the REAL scan path (the canonical redactor is not
#: importable), not a rare fallback. 20 base64 chars decode to ~15 bytes -- long enough to
#: carry a short credential, short enough that a bare word is not decoded as one.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")

#: Ceiling on how much of one text is decoded, so a large file cannot turn the scan into the
#: build's slowest step. Runs are examined longest-first, because a credential plus its label
#: is longer than a bare token and the long runs are the ones worth the budget.
_B64_DECODE_BUDGET = 256 * 1024


def _scan_decoded_runs(text: str, origin: str) -> list[Leak]:
    """Findings from base64 runs in *text*, judged by the same patterns as the literal pass.

    Not recursive: one decode. A credential wrapped twice is out of scope here and stays with
    the canonical redactor, which is preferred whenever it can be imported.
    """
    found: list[Leak] = []
    spent = 0
    skipped_unscanned = 0
    # Longest first, because a credential plus its label is longer than a bare token, so the
    # long runs are the ones worth the budget.
    #
    # ``continue`` and NOT ``break``. This was ``break``, and combined with that ordering it
    # made a single oversized run disable the scan completely: the longest run is examined
    # first, so if it alone exceeded the budget the loop exited before reading anything, and
    # every shorter run -- including the one carrying the credential -- went unscanned. A
    # blob big enough to trip the ceiling is trivially easy to include, which turned a memory
    # bound into an off switch.
    for match in sorted(_B64_RUN_RE.finditer(text), key=lambda m: -len(m.group(0))):
        run = match.group(0)
        if spent + len(run) > _B64_DECODE_BUDGET:
            # FAIL CLOSED. ``continue`` alone was still a silent pass: a credential inside a
            # run past the budget went unscanned and the output said the content was clean.
            # ``break`` was worse (one oversized run disabled everything) but both shared the
            # same flaw -- unscanned reported as scanned. A Leak is appended instead, so the
            # build refuses and names what it could not read.
            skipped_unscanned += 1
            continue
        spent += len(run)
        try:
            raw = base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
            decoded = raw.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError):
            # Not base64, or not text once decoded. Either way there is nothing here that the
            # literal patterns could read, so it is not a finding.
            continue
        for kind, pattern in _HARD_PATTERNS:
            hit = pattern.search(decoded)
            if hit:
                token = hit.group(0)
                found.append(
                    Leak(
                        origin=origin,
                        kind=f"encoded-{kind}",
                        line=text.count("\n", 0, match.start()) + 1,
                        snippet=token[:4] + "…(%d chars, base64)" % len(token),
                    )
                )
    if skipped_unscanned:
        found.append(
            Leak(
                origin=origin,
                kind="unscannable-encoded",
                line=0,
                snippet=(
                    "%d base64 run(s) past the %d byte decode budget were NOT scanned"
                    % (skipped_unscanned, _B64_DECODE_BUDGET)
                ),
            )
        )
    return found

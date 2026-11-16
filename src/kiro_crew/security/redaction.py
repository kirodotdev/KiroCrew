"""Credential redaction on every output path.

This is the OUTPUT side of the module, and the widest external surface in it:
the batch redactors run on every path that persists, displays or forwards text,
so a name here is called from most of the codebase rather than from one caller.

The credential plan (:func:`_credential_redaction_plan`) is the rule table in
:mod:`kiro_crew.security.redaction_structured`; it masks only a credential's VALUE and
leaves everything else as written. This module keeps the batch redactor with that plan
and its records, the owner's ``token`` parameter exemption
(:func:`_exempt_token_values`), the redaction tags and the path passes.

The fixed credential alternation and its pre-filter live in ``redaction_patterns``
and the shape gates in ``redaction_entropy``, owner modules beside this one. No
redaction path reads them: they serve the request-BLOCKING decisions
(``exfil._contains_fixed_credential`` and its siblings) and the publish-time content
scans. This module is the import path of every name they define; a read through it
answers with the owner's object, and a write reaches every owner that binds the name
(:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import importlib as _importlib
import posixpath
import re
import secrets
import sys as _sys
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.credential_patterns import (  # noqa: F401
    _HTML_REF_AMP,
    _HTML_REF_EQUALS,
    _HTML_REF_QUEST,
    _TOKEN_PARAM_EQ_RE,
    _TOKEN_PARAM_NAME_RE,
    _TOKEN_PARAM_NAME_SPELLINGS,
    _TOKEN_PARAM_SEP_ENTITY_RE,
    _TOKEN_PARAM_SEP_RE,
    _TOKEN_PARAM_VALUE_CLASS,
    AWS_KEY_ID,
    TOKEN_PARAMETER,
)
from kiro_crew.security.redaction_switch import credential_pass_bypassed

# ``kiro_crew.security`` imports this name from here.
from .redaction_patterns import _might_contain_credential  # noqa: F401

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
#: Every tag a redactor writes is one of these: the URL pass
#: (:func:`redact_exfiltration_urls`) masks a credential value inside a URL with
#: the same credential tag, so "was this text rewritten" is answered by this tuple.
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


# ── `?token=` / `&token=` URL parameter anchors ──
# The rule table reads a URL's ``token`` parameter (row ``url.token_param.token``).
# These patterns only show ``StreamRedactor.feed`` where a ``token=`` anchor, or the
# prefix of one, sits at a chunk's end, so the stream holds it (``text_redaction``);
# none of them decides what is masked. The spelling lives in ``credential_patterns``
# (`TOKEN_PARAMETER`), which the log-redaction floor compiles too.
_TOKEN_PARAM_NAME_PREFIX_RE = (
    "(?:"
    + "|".join(
        "".join(c for c, _ in _TOKEN_PARAM_NAME_SPELLINGS[:k]) + _TOKEN_PARAM_NAME_SPELLINGS[k][1]
        for k in range(len(_TOKEN_PARAM_NAME_SPELLINGS))
    )
    + ")"
)
_TOKEN_PARAM_RE = re.compile(TOKEN_PARAMETER)

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


#: A well-formed companion prefix: ``https://host/path`` with no whitespace, query or
#: fragment. One without a path is dropped, since a bare host matches ``host.evil.example``.
_TOKEN_PARAM_EXEMPT_PREFIX_RE = re.compile(r"https://[^\s/?#&]+/[^\s?#&]*")

# A streaming window cannot prove the whole-message preceding-context guard.
_TOKEN_PARAM_EXEMPT_SUSPENDED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "token_param_exempt_suspended", default=False
)


def _token_param_exempt_prefixes() -> tuple[str, ...]:
    """Companion ``https://host/path`` prefixes whose ``?token=`` value the plan keeps.

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
    """The members of *prefixes* the exemption may honour, sorted; malformed ones drop.

    The one shape gate for the live read and for a caller's snapshot alike.
    """
    members = (p for p in prefixes if isinstance(p, str))
    return tuple(sorted(p for p in members if _TOKEN_PARAM_EXEMPT_PREFIX_RE.fullmatch(p)))


#: Single-character wrappers that may open an exempt URL. A ``(`` is accepted only
#: as the second half of a markdown ``](`` opener by ``_TokenParamExemptScan.boundary``.
_TOKEN_PARAM_EXEMPT_OPENERS = frozenset("<\"'`*")
_TOKEN_PARAM_EXEMPT_WHITESPACE = " \t\r\n"

#: An exempt value is ONE run of URL-unreserved, base64 or percent-encoded bytes plus
#: sentence/markdown-link closers, read up to the first whitespace or byte of
#: :data:`_TOKEN_PARAM_RUN_STOPS`; a run holding any other byte (a second URL glued after
#: the token, ``?token=k?token=SECRET``) is redacted.
_TOKEN_PARAM_EXEMPT_VALUE_RE = re.compile(r"[A-Za-z0-9._~%+/=-]+")
_TOKEN_PARAM_EXEMPT_TRAIL = frozenset(")].,;:!?*")
_TOKEN_PARAM_RUN_STOPS = frozenset("&\"'#<>{}|\\^`")
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
    """The two whole-message guards of the exemption, for ONE text's ``token`` values.

    Both guards are stated against the text before a candidate -- the boundary
    guard against the whitespace-free run ending at the prefix, the destination
    guard against everything earlier with proven exempt URLs blanked -- and
    `_credential_redaction_plan` visits candidates left to right. So each guard
    keeps a cursor and reads every character once, which keeps the exemption linear
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

    __slots__ = (
        "text",
        "cursor",
        "signal_seen",
        "sep",
        "ws_cursor",
        "last_ws",
        "left_floor",
        "run_to",
    )

    def __init__(self, text: str) -> None:
        self.text = text
        self.cursor = 0  # end of the last exempt URL: text before it is blanked or clear
        self.signal_seen = False
        self.sep = -1  # the `?` of the match whose candidates are being tried
        self.ws_cursor = 0
        self.last_ws = -1  # last whitespace before `sep`
        self.left_floor = 0  # a boundary below it has an earlier matched prefix in `left`
        self.run_to = -1  # end of the last value run read; later values inside it share it

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

    def run_end(self, start: int) -> int:
        """End of the value run from *start* (module ``_TOKEN_PARAM_EXEMPT_VALUE_RE``).

        Values come in text order, so one inside the last run read ends where it does.
        """
        if start < self.run_to:
            return self.run_to
        text = self.text
        i = start
        while i < len(text) and not (text[i] in _TOKEN_PARAM_RUN_STOPS or text[i].isspace()):
            i += 1
        self.run_to = i
        return i

    def accept(self, exempt_url: tuple[int, int]) -> None:
        """Record an exempt URL; its span is blanked for every later candidate."""
        self.cursor = exempt_url[1]


_TOKEN_PARAM_ANCHOR = "?token="


def _token_param_exempt(
    text: str,
    value_start: int,
    prefixes: tuple[str, ...],
    scan: _TokenParamExemptScan,
) -> tuple[int, int] | None:
    """Return the exempt URL span for the ``token`` value starting at *value_start*."""
    sep = value_start - len(_TOKEN_PARAM_ANCHOR)
    if sep < 0 or not text.startswith(_TOKEN_PARAM_ANCHOR, sep):
        return None
    run = _TOKEN_PARAM_EXEMPT_VALUE_RE.match(text, value_start)
    if run is None:
        return None
    if not set(text[run.end() : scan.run_end(value_start)]) <= _TOKEN_PARAM_EXEMPT_TRAIL:
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


def _exempt_token_values(
    text: str,
    values: list[tuple[int, int]],
    defer_token_param: bool,
    token_param_prefixes: tuple[str, ...] | None = None,
) -> set[tuple[int, int]]:
    """The ``token`` URL parameter values the plan leaves as written; a subset of *values*.

    *values* are the ``(start, end)`` spans, in text order, that the table's
    ``url.token_param.token`` row alone reads. None is kept while a stream window is
    read (the suspension pins the prefix set empty) or while the set is empty.
    Otherwise a deferred delta (``defer_token_param``) keeps every one, for the
    caller's whole-segment run, and a whole text keeps each one an approved companion
    URL carries (:func:`_token_param_exempt`), such as an approval link the user must
    click. ``token_param_prefixes`` is a caller's snapshot of the set; ``None`` reads
    the live set, and only once such a value exists.
    """
    if not values or _TOKEN_PARAM_EXEMPT_SUSPENDED.get():
        return set()
    if token_param_prefixes is None:
        prefixes = _token_param_exempt_prefixes()
    else:
        prefixes = _well_formed_token_param_prefixes(token_param_prefixes)
    if not prefixes:
        return set()
    if defer_token_param:
        return set(values)
    scan = _TokenParamExemptScan(text)
    kept: set[tuple[int, int]] = set()
    for start, end in values:
        exempt_url = _token_param_exempt(text, start, prefixes, scan)
        if exempt_url is not None:
            scan.accept(exempt_url)
            kept.add((start, end))
    return kept


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
    delta cannot show the exemption the companion prefix in front of a ``?token=``,
    so while ``CredentialPolicy.token_param_exempt_url_prefixes()`` is non-empty a
    ``token=`` value only the table's token row reads is left to that whole-segment
    run (:func:`_exempt_token_values`); a value another row reads is still redacted
    here. With the empty default prefix set the output is byte-identical to a plain
    call.

    ``token_param_prefixes`` is a SNAPSHOT of that prefix set that stands in for
    the exemption's live read. A caller that keys a cache on the set passes the set it
    keyed on, so the output is computed under exactly that set. ``None``, the
    default, is the live read.
    """
    spans, warnings, _rules = _credential_redaction_plan(
        text, defer_token_param=defer_token_param, token_param_prefixes=token_param_prefixes
    )
    if not spans:
        return text, warnings
    return _splice(text, spans), warnings


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
    text: str,
    *,
    defer_token_param: bool = False,
) -> tuple[str, list[str], list[CredentialMatch]]:
    """:func:`redact_credentials`, plus one :class:`CredentialMatch` per tag written.

    The cleaned text and warnings are byte-identical to ``redact_credentials``
    with the same ``defer_token_param``; both share one plan, so the records
    cannot describe a redaction the text does not contain.
    A whole-text fail-closed placeholder's record carries an empty ``value``.
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
        value = "" if rule == REDACTION_UNAVAILABLE_RULE else text[start:end][len(label) :]
        matches.append(CredentialMatch(ordinal, rule, label, value))
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

    The rule table (:mod:`kiro_crew.security.redaction_structured`) decides every
    credential: each Secret hit's VALUE is replaced and the label, header name or URL
    around it stays (:func:`~kiro_crew.security.redaction_structured.compat.credential_plan`),
    except a ``token`` parameter value the owner's companion URLs keep
    (:func:`_exempt_token_values`). Every span is positioned against the IMMUTABLE
    input and the string is rewritten exactly once at the end.

    Unconditional on every surface EXCEPT inside an explicit
    ``redaction_switch.owner_view()`` scope, or an output seam opened while the
    owner's setting is off (``redaction_seams.output_seam``): there it returns no
    spans. A caller that has not opened a scope -- every admission gate, every
    refusal check -- gets the full plan whatever the owner chose.
    """
    if credential_pass_bypassed():
        return [], [], {}

    # Lazy: the detectors' seam imports this module's tags.
    from kiro_crew.security.redaction_structured import compat

    return compat.credential_plan(text, defer_token_param, token_param_prefixes)


#: The ``(rule, label)`` and text of a whole-text placeholder: the detectors could not run.
REDACTION_UNAVAILABLE_RECORD = ("redaction_unavailable", "")
REDACTION_UNAVAILABLE_RULE = REDACTION_UNAVAILABLE_RECORD[0]
REDACTION_UNAVAILABLE_TAG = "[REDACTED: redaction unavailable]"


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
        _B64_CHUNK_RE,
        _BARE_SECRET_RUN_RE,
        _ENTROPY_TERMS_KEY_LEN,
        _EXCHANGE_GUID_DASHES,
        _EXCHANGE_ID_MAX_BYTES,
        _HEX_ONLY_RE,
        _HOST_ID_EXEMPT_OVERLAP,
        _LINK_ALNUM,
        _LINK_HOST_CHARS,
        _LINK_PATH_CHARS,
        _LINK_PATH_MAX,
        _OUTLOOK_FOLDERS,
        _OUTLOOK_HOSTS,
        _OUTLOOK_ID_ESCAPES,
        _OUTLOOK_ID_PREFIXES,
        _OWA_QUERY_CHARS,
        _OWA_WEB_LINK_PATH,
        _OWA_WEB_LINK_QUERIES,
        _PRINTABLE_BYTES,
        _ROUTED_COMMIT_PIECE_MIN,
        _ROUTED_COMMIT_RE,
        _SECRET_ENTROPY_MIN,
        _SECRET_KEY_LEN,
        _SECRET_MAX_LOWER_RUN,
        _SECRET_MAX_SLASHES,
        _SECRET_MAX_VOWEL_RATIO,
        _SECRET_PRINTABLE_DECODE_RATIO,
        _URL_PATH_SEP,
        _VOWELS,
        _bare_secret_window_starts,
        _char_runs,
        _chat_mail_link_at,
        _chat_mail_link_pieces,
        _chat_mail_link_spans,
        _contains_bare_secret,
        _contains_fixed_credential,
        _crosses_into_a_routed_commit,
        _decode_b64_chunk,
        _decode_b64_safe,
        _decodes_to_printable_text,
        _escapes_as_slash,
        _expand_exchange_runs,
        _has_all_three_char_classes,
        _is_guid_text,
        _is_item_entry_id,
        _is_outlook_id,
        _is_slack_client_route,
        _is_slack_host,
        _is_whole_exchange_id,
        _looks_like_secret_key,
        _lowercase_run_exceeds,
        _outlook_item_id,
        _outlook_web_link_item,
        _outlook_web_link_tail,
        _shannon_entropy,
        _text_contains_bare_secret,
        _vowel_ratio,
    )
    from kiro_crew.security.redaction_patterns import (  # noqa: F401
        _ARN_ACCOUNT_ID_12_DIGITS_RE,
        _ARN_ACCOUNT_ID_PREFIX_RE,
        _ARN_PREFIX_MAX_LOOKBACK,
        _BOT_ID_LENGTHS,
        _CDK_NAME_MAX_LEN,
        _CDK_RESOURCE_NAME_RE,
        _CDK_WORDS,
        _CREDENTIAL_PATTERNS,
        _CREDENTIAL_PATTERNS_NO_PEM_BLOCK,
        _CREDENTIAL_PATTERNS_SANS_JWT,
        _CREDENTIAL_PATTERNS_SANS_TELEGRAM,
        _CREDENTIAL_PREFILTER_AUTHORIZATION_RE,
        _CREDENTIAL_PREFILTER_AUTHORIZATION_UPPER_RE,
        _CREDENTIAL_PREFILTER_DISCORD_RE,
        _CREDENTIAL_PREFILTER_GH_RE,
        _CREDENTIAL_PREFILTER_LITERALS,
        _CREDENTIAL_PREFILTER_TELEGRAM_RE,
        _CREDENTIAL_PREFILTER_URI_RE,
        _CREDENTIAL_STARTS_SANS_JWT,
        _JWT_SEGMENT_RUN_RE,
        _NATIVE_GATE,
        _PREFILTER_MIN_LEN,
        _TELEGRAM_AT_RE,
        _TELEGRAM_BRANCH,
        _TELEGRAM_DIGITS_RE,
        _arn_exemption_withheld,
        _contains_credential_pattern,
        _credential_matches,
        _credential_search,
        _jwt_branch_matches_at,
        _load_native_gate,
        _might_contain_credential_py,
        _native_gate_fingerprint,
        _next_jwt_branch_start,
        _sans_jwt_start,
        _segment_run_end,
        get_credential_patterns,
        iter_credential_matches,
    )
if TYPE_CHECKING:  # project names it serves from outside its owners, from their homes
    import kiro_crew.security.pem_scan as pem_scan  # noqa: F401
    from kiro_crew.credential_patterns import (  # noqa: F401
        JWT_MULTI_SEGMENT,
    )
    from kiro_crew.jwt_header import (  # noqa: F401
        is_jwt_header,
    )

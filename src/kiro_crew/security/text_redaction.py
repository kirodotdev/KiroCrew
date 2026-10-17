"""The package's redaction entry points: whole text, audit samples and streams.

:func:`redact` and :func:`redact_with_findings` run the two baseline passes over a
whole string in their one correct order, exfiltration URLs before credentials.
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

from kiro_crew.security import _submodule

from . import redaction

if TYPE_CHECKING:
    from collections.abc import Callable


def redact_with_findings(text: str) -> tuple[str, list[str], list[str]]:
    """Apply all redaction passes, reporting what each one removed.

    The same passes in the same order as :func:`redact`, which is the point of
    it living here: exfiltration URLs run FIRST because that pass matches whole
    URLs, and a credential replaced ahead of it leaves a placeholder inside one,
    which the URL matcher then fails to recognise as the shape it is there to
    catch. A caller that wants the found lists would otherwise hand-sequence
    the two calls and own that ordering separately -- which several already do,
    in the reverse order.

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
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _exfil = _submodule("exfil")
    _redaction = _submodule("redaction")

    text, urls = _exfil.redact_exfiltration_urls(text)
    text, credentials = _redaction.redact_credentials(text)
    return text, list(credentials), list(urls)


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
    string.ascii_letters + string.digits + "_-+/=.:@%~" + '"' + "'" + "?&#"
)

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
    rf"(?:Bearer(?:\s+{_BEARER_VALUE_CLASS}*)?|Beare|Bear|Bea|Be|B)?\Z",
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


class StreamRedactor:
    """Rolling-buffer redactor for streamed LLM output.

    Feed raw chunks in order; ``feed`` returns the redacted, safe-to-broadcast
    prefix (possibly empty while a partial credential is buffered). Call
    ``flush`` when the stream/segment ends to redact and return the remainder.
    Adds at most one chunk of latency. A credential is never split across a
    commit boundary because commits only ever end at a non-credential-class
    character, while a credential is a contiguous credential-class run.
    """

    __slots__ = ("_buf", "_redact", "_discarding", "_discard_kind", "_discarded")

    def __init__(self, redactor: "Callable[[str], str] | None" = None) -> None:
        self._buf = ""
        # Resolve at call time so module-load order is irrelevant.
        self._redact = redactor or redact
        self._discarding = False
        self._discard_kind: str | None = None
        self._discarded = 0

    def _redact_window(self, text: str) -> str:
        """Redact one context-incomplete window without URL-prefix exemptions."""
        redaction = _submodule("redaction")
        suspension = redaction._TOKEN_PARAM_EXEMPT_SUSPENDED.set(True)
        try:
            return self._redact(text)
        finally:
            redaction._TOKEN_PARAM_EXEMPT_SUSPENDED.reset(suspension)

    def feed(self, chunk: str) -> str:
        """Accept a chunk; return the redacted prefix that is safe to emit now."""
        # Owners of the re-exported names read below, resolved through the
        # import system so the value is read from the one place it lives.
        _redaction = _submodule("redaction")

        if not chunk:
            return ""
        self._buf += chunk

        # Invariant: `_buf` is always "" on entry when `_discarding` is true;
        # this chunk is solely the continuation of the already-tagged drop.
        # Only a terminator byte exits the discard. Reaching the bound with no
        # terminator re-emits the tag and resets the counter but stays armed:
        # clearing the flag there would hand the credential's remaining bytes
        # to Phase A as an anchorless run, which the 512 floor streams raw.
        if self._discarding:
            assert self._discard_kind is not None
            run_match = _STREAM_DISCARD_RUN_RES[self._discard_kind].match(self._buf)
            assert run_match is not None
            run = run_match.end()
            self._discarded += run
            if run == len(self._buf):
                self._buf = ""
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
        natural_cut = len(self._buf)
        while natural_cut > 0 and self._buf[natural_cut - 1] in _CRED_CLASS:
            natural_cut -= 1
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
        if natural_cut > 0 and _PEM_HOLD_RE.search(
            self._buf[max(0, natural_cut - 50) : natural_cut]
        ):
            safety_cuts.append(0)

        # Bearer anchors are STRONG: their embedded whitespace is not in
        # _CRED_CLASS, so the natural cut could otherwise split anchor and value.
        bearer_anchor = _BEARER_ANCHOR_PARTIAL_RE.search(self._buf)
        if bearer_anchor is not None:
            safety_cuts.append(bearer_anchor.start())

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

        i = min(safety_cuts)
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
        if len(self._buf) - i > cap and strong_anchored:
            cap = _STREAM_HOLDBACK_JWT_MAX
        if len(self._buf) - i > cap:
            if strong_anchored:
                # Preserve the fail-closed ceiling for a real credential anchor:
                # redact the safe prefix, tag the event, and drop the oversized
                # tail rather than bisecting it and exposing the token head.
                # Only STRONG evidence arms the sticky discard. A WEAK trailing
                # name prefix (`&tok`, no `=`) is held by the safety cut but is
                # not yet a credential: it authorizes no drop and names no value
                # class to drop with.
                credential_reaches_end = (
                    partial_jwt is not None
                    or bearer_anchor is not None
                    or strong_token_anchor
                    or (
                        complete_token_crossing is not None
                        and complete_token_crossing.end(1) == len(self._buf)
                    )
                )
                self._discarding = credential_reaches_end
                self._discard_kind = None
                if credential_reaches_end:
                    # Every arming term above maps to exactly one kind, carrier
                    # first: a STRONG token anchor or a complete crossing ending
                    # the buffer -> "token-param"; a Bearer anchor -> "bearer";
                    # a partial JWT -> "jwt". No other term can arm, so the
                    # final `else` holds a true invariant.
                    # Prefer an enclosing carrier over a token shape inside its
                    # value: its value class defines where that credential ends.
                    token_param_reaches_end = strong_token_anchor or (
                        complete_token_crossing is not None
                        and complete_token_crossing.end(1) == len(self._buf)
                    )
                    if token_param_reaches_end:
                        self._discard_kind = "token-param"
                    elif bearer_anchor is not None:
                        self._discard_kind = "bearer"
                    else:
                        assert partial_jwt is not None
                        self._discard_kind = "jwt"
                self._discarded = 0
                # A complete value may cross the safety cut yet end before a
                # benign suffix in the same buffer. Drop only through the value;
                # the suffix remains buffered for normal processing. Sticky
                # discard is reserved for credential material that reaches the
                # buffer end, where a continuation can still arrive.
                drop_end = len(self._buf)
                if complete_token_crossing is not None and not credential_reaches_end:
                    drop_end = complete_token_crossing.end(1)
                commit, self._buf = self._buf[:i], self._buf[drop_end:]
                out = self._redact_window(commit) if commit else ""
                return out + _redaction._REDACTED_CREDENTIAL_TAG
            i = len(self._buf) - cap
            i -= self._buf.startswith("${", i - 1)  # keep a JS/TS `${` placeholder whole

            # The floor was computed after Phase A, so re-check complete token
            # parameters against the actual cut. Advancing through the value is
            # safe because the whole parameter reaches one batch-redaction call,
            # and it shrinks the buffer rather than weakening the DoS bound.
            floor_crossing = _complete_token_match_crossing(complete_token_matches, i)
            if floor_crossing is not None:
                i = floor_crossing.end(1)

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

    def flush(self) -> str:
        """Redact and return the buffered remainder; clears the buffer."""
        if self._discarding:
            self._buf = ""
            self._discarding = False
            self._discard_kind = None
            return ""
        out = self._redact_window(self._buf) if self._buf else ""
        self._buf = ""
        return out

    @property
    def held(self) -> str:
        """The raw run withheld so far (``""`` while a value is being dropped)."""
        return self._buf

    @property
    def discarding(self) -> bool:
        """True while the rest of a value is being dropped."""
        return self._discarding

    def reset(self) -> None:
        """Discard the buffer without emitting (segment abandoned/cleared)."""
        self._buf = ""
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
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _exfil = _submodule("exfil")
    _redaction = _submodule("redaction")

    return _redaction.redact_credentials(_exfil.redact_exfiltration_urls(text or "")[0])[0][
        :max_chars
    ]

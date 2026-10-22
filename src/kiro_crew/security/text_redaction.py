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
    "aws-keyed": re.compile(rf"{stream_aws_anchor.VALUE_CLASS}*"),
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
        for match in _submodule("redaction")._credential_matches(buf):
            if match.start() >= key_start:
                break
            inside = inside or key_start < match.end()
            spans.append(match.span())
        # Latest start first, so a span that crosses an earlier move is seen too.
        # A match can start mid-token, so each move walks back to whitespace
        # again: a bare secret glued in front of the match stays whole.
        for start, end in sorted(spans, reverse=True):
            if start < cut < end:
                cut = _token_start(buf, start)
    return cut, inside


def _token_start(buf: str, pos: int) -> int:
    while pos > 0 and not buf[pos - 1].isspace():
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

    __slots__ = ("_buf", "_redact", "_discarding", "_discard_kind", "_discarded")

    def __init__(self, redactor: "Callable[[str], str] | None" = None) -> None:
        self._buf = ""
        # Resolve at call time so module-load order is irrelevant.
        self._redact = redactor or redact
        self._discarding = False
        self._discard_kind: tuple[str, ...] | None = None
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
            if run == len(self._buf):
                # Keep the tail still in the discard, so a label split across
                # chunks is seen whole on the next pass.
                carry = stream_aws_anchor.LABEL_CARRY if "aws-keyed" in kinds else 0
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
                for match in _redaction._credential_matches(self._buf):
                    if match.start() < i < match.end():
                        i = match.end()

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
        return "" if self._discarding else self._buf

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
    return redact_with_findings(text or "")[0][:max_chars]

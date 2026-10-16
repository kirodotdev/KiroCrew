"""Linear PEM private-key detection for content scanners.

The canonical combined credential regex (:func:`get_credential_patterns`) carries
one PEM branch of the shape::

    -----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\\s\\S]*?-----END ...-----|...)

The leading ``[\\s\\S]*?`` is a lazy scan that, when no matching END header
exists ahead, walks forward to the end of the string. A regex engine restarts
that walk at every ``BEGIN`` header, so iterating that pattern over content
holding many ``BEGIN PRIVATE KEY`` headers with no matching END is O(n^2): a
text file previewed through the design_tweak backend with a few thousand such
headers takes seconds, and a pre-publish deploy scan repeats the walk per header
up to its findings cap.

That branch is essential to redaction (it spans the key body so it can be
masked), so it stays untouched there. Content *scanners* only need a boolean
"does this contain a private key?", which a single linear forward pass answers.
This module provides that pass plus a sans-PEM view of the combined pattern, so
a scanner runs the (non-pathological) non-PEM branches through the normal regex
and the PEM check through :func:`contains_pem_private_key`.

The PEM check reuses the EXACT original PEM-branch regex text, so its accept /
reject decision is identical to the canonical pattern's -- a real (complete or
truncated) key is refused, while a bare quoted marker, a quoted BEGIN/END pair
with no body, and an inline marker followed by prose stay allowed. Linearity is
obtained only by *where* the regex is applied: it is anchored at each BEGIN
header, and the lazy END-seeking alternative is skipped whenever no END header
is reachable ahead (precomputed once), which is exactly the pathological case.
"""

from __future__ import annotations

import bisect
import re

from .redaction import get_credential_patterns

__all__ = [
    "CREDENTIAL_PATTERNS_SANS_PEM",
    "contains_pem_private_key",
]

# The exact PEM branch literal as it appears, once, inside the combined pattern.
# Kept in sync with redaction.py by the assertion in _build_sans_pem(): if the
# branch text ever changes there, the strip becomes a no-op and import fails
# loudly rather than leaving a silently-still-quadratic scanner.
_PEM_BRANCH = (
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:"
    r"[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?:\r?\n(?:Proc-Type:[^\n]*|DEK-Info:[^\n]*|[A-Za-z0-9+/=]+(?=\r?\n|\Z)"
    r"|(?=\r?\n[A-Za-z0-9+/=])))*"
    r")"
)

# The same PEM branch WITHOUT the leading top-level ``|``. The body decision it
# encodes -- a header counts only when armoured key material follows it -- is
# preserved exactly below. Only the *complete-block* alternative's lazy
# ``[\s\S]*?-----END-----`` is replaced (it is the sole quadratic part, scanning
# to end-of-string once per BEGIN when no END is reachable); the truncated-block
# alternative is kept verbatim because it is already local to the header and so
# linear.
_PEM_TRUNCATED_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:\r?\n(?:Proc-Type:[^\n]*|DEK-Info:[^\n]*|[A-Za-z0-9+/=]+(?=\r?\n|\Z)"
    r"|(?=\r?\n[A-Za-z0-9+/=])))*"
)

# The complete-block alternative, verbatim: BEGIN then the lazy span to the first
# END header. Applied only when an END is reachable (so the lazy scan is bounded
# by that END, never a walk to end-of-string), and the match cursor advances past
# it, so the whole pass stays linear. Reusing the regex keeps the complete-block
# accept/reject decision byte-identical to the canonical branch instead of
# reconstructing it with position arithmetic.
_PEM_COMPLETE_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)

# Markers are stripped before the body check; armoured body is a >=16-char
# base64 run -- identical to the design_tweak preview rule. PEM markers hold no
# such run themselves (dashes and spaces break a base64 run), but stripping is
# kept so the check is textually identical to the original and obviously safe.
_PEM_MARKER_RE = re.compile(r"-{5}(?:BEGIN|END)[A-Z0-9 ]*PRIVATE KEY-{5}")
_PEM_BODY_RE = re.compile(r"[A-Za-z0-9+/=]{16,}")

_END_HEADER_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def _build_sans_pem() -> re.Pattern[str]:
    (combined,) = get_credential_patterns()
    pattern = combined.pattern
    if pattern.count(_PEM_BRANCH) != 1:
        raise AssertionError(
            "pem_fast: the canonical PEM branch does not appear exactly once in "
            "the combined credential pattern; update _PEM_BRANCH to match "
            "redaction.py so the sans-PEM scanner stays correct and linear."
        )
    return re.compile(pattern.replace(_PEM_BRANCH, "", 1), combined.flags)


#: The combined credential pattern with the one quadratic PEM branch removed.
#: Content scanners iterate this for the non-PEM branches and use
#: :func:`contains_pem_private_key` for the PEM case.
CREDENTIAL_PATTERNS_SANS_PEM: re.Pattern[str] = _build_sans_pem()

#: Linear anchor for a PEM private-key header. A plain literal with no unbounded
#: quantifier, so ``finditer`` over it is O(n).
_PEM_HEADER_RE: re.Pattern[str] = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _span_has_body(span: str) -> bool:
    """Whether a matched PEM span carries armoured key material (markers stripped)."""
    return _PEM_BODY_RE.search(_PEM_MARKER_RE.sub("", span)) is not None


def contains_pem_private_key(text: str) -> bool:
    """Return whether *text* holds a PEM private key backed by key material.

    The accept / reject decision is byte-identical to the canonical PEM branch:
    the branch's own two alternatives are reused verbatim as :data:`_PEM_COMPLETE_RE`
    (``BEGIN`` lazily to the first ``END``) and :data:`_PEM_TRUNCATED_RE`
    (``BEGIN`` then its contiguous base64 / metadata lines). Only *where* they run
    is changed, to keep the scan O(n):

    * The complete-block regex's lazy ``[\\s\\S]*?-----END-----`` is run only when
      an ``END`` header is reachable at or after this ``BEGIN`` marker's end
      (checked once against a sorted list of ``END`` starts). Requiring the END to
      start at/after the marker's end means an ``END`` marker whose leading dashes
      overlap a ``BEGIN`` marker's own trailing dashes cannot be read as the
      terminator -- so a crafted header that runs a ``BEGIN`` marker straight into
      an ``END`` marker with no gap does not fool the scan into an empty block and
      let a real key through. With no reachable END the lazy alternative cannot
      match, so it is skipped and only the truncated regex runs -- the lazy scan
      never walks to end-of-string.
    * A monotonic cursor (``reached``) records how far the last accepted match
      consumed; a ``BEGIN`` that starts inside that span is skipped. A header
      quoted inside a ``Proc-Type:`` continuation line is therefore read once as
      part of its block, not re-matched from, so nesting headers in metadata
      lines cannot make the pass quadratic.

    A header counts only when its matched span carries a ``>=16``-char base64 run
    (markers stripped), so a bare quoted marker, a quoted BEGIN/END pair with
    nothing between, and an inline marker followed by prose are all allowed; a
    real (complete or truncated) key is refused.
    """
    begins = [(m.start(), m.end()) for m in _PEM_HEADER_RE.finditer(text)]
    if not begins:
        return False
    end_starts = [m.start() for m in _END_HEADER_RE.finditer(text)]

    reached = -1  # furthest offset an accepted match has consumed.
    for begin_start, begin_end in begins:
        if begin_start < reached:
            continue
        # Prefer the complete-block form when an END is reachable at/after this
        # marker's end; its lazy scan then stops at that END (bounded). The
        # at/after-marker-end requirement rejects an END overlapping the BEGIN
        # marker's trailing dashes.
        if bisect.bisect_left(end_starts, begin_end) < len(end_starts):
            m = _PEM_COMPLETE_RE.match(text, begin_start)
            if m is not None:
                reached = max(reached, m.end())
                if _span_has_body(m.group()):
                    return True
                continue
            # The complete alternative failed (the only END lies inside this
            # marker's own dashes); fall through to the truncated rule, exactly
            # as the canonical branch's alternation does.
        m = _PEM_TRUNCATED_RE.match(text, begin_start)
        if m is not None:
            reached = max(reached, m.end())
            if _span_has_body(m.group()):
                return True
    return False

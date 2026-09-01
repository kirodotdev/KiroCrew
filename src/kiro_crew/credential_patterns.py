"""Credential regex spellings shared by the log-redaction leaf and the scrubber.

``log_redaction`` installs at CLI bootstrap, before heavy modules load, so it
cannot import ``security``. The two AWS-key-ID and JWT spellings were therefore
written out twice -- once in each home -- and parity was held only by tests that
read ``security.py`` as SOURCE TEXT and grepped it for a literal. That guard
fires on a rename, not on a divergence in meaning, and it costs a test per
duplicated pattern.

This module is the single home for those spellings. Both consumers import from
here, so there is nothing left to keep in sync.

Why fragments rather than compiled patterns
-------------------------------------------
Everything below is pattern SOURCE. ``security.py`` embeds the AWS spelling in
five differently shaped patterns -- three plain alternation branches, one
``X-Amz-Credential=`` presigned-URL matcher and one fully anchored ``^...$``
parameter validator -- and each needs its own anchors, continuations and compile
flags. A compiled pattern could not be reused at four of those five sites.

Why this module imports NOTHING
-------------------------------
Not even ``re``, and not even ``from __future__ import annotations`` -- there is
nothing here to annotate. ``log_redaction`` is a deliberate import leaf -- it is
installed before the heavy modules load -- so anything it imports is on the
bootstrap path. Exporting plain strings keeps this module's import cost at zero
and makes it impossible for a future edit here to drag a dependency into that
path. ``test_credential_patterns.py`` pins the import-free shape.

The two AWS spellings deliberately DIVERGE
------------------------------------------
The redaction floor matches two prefixes the scrubber does not. Over-matching is
the fail-closed direction for redaction: a false positive costs one masked log
line, while a miss writes a live key to disk. The scrubber's narrower spelling is
load-bearing in the other direction -- it gates request-blocking decisions and a
cheap literal pre-filter whose anchors must stay a superset of every branch (see
``_CREDENTIAL_PATTERNS`` in ``security.py``), so widening it is a security
behaviour change and not a refactor.

That asymmetry is preserved and made explicit here: ``AWS_KEY_ID_REDACTION`` is
BUILT from the same prefix and body fragments as ``AWS_KEY_ID`` plus named extra
prefixes. The superset relation now holds by construction, so no test has to
compare two hand-written strings to discover whether it still does.
"""

#: AWS access key-ID prefixes both consumers match: long-term (``AKIA``) and
#: temporary/STS (``ASIA``) ids.
AWS_KEY_ID_PREFIXES = "AKIA|ASIA"

#: Prefixes matched by the REDACTION floor only -- ``ABIA`` (bearer tokens) and
#: ``ACCA`` (context-specific credentials). Kept out of ``AWS_KEY_ID`` because
#: adding them to the scrubber widens what a security surface blocks and needs
#: matching pre-filter anchors; see the module docstring.
AWS_KEY_ID_REDACTION_ONLY_PREFIXES = "ABIA|ACCA"

#: The id body: exactly 16 uppercase alphanumerics after the 4-letter prefix.
#: Precise enough for near-zero false positives, and the id is the searchable
#: half of a leaked AWS credential pair.
AWS_KEY_ID_BODY = "[A-Z0-9]{16}"

#: AWS access key ID as the credential scrubber matches it.
AWS_KEY_ID = f"(?:{AWS_KEY_ID_PREFIXES}){AWS_KEY_ID_BODY}"

#: AWS access key ID as the log-redaction floor matches it: a strict superset of
#: :data:`AWS_KEY_ID`, by construction.
#:
#: No word-boundary assertions here or in :data:`AWS_KEY_ID`: a key rendered
#: adjacent to word characters (``aws_AKIA...``, ``key=AKIA...x``) has no ``\b``
#: between ``_`` and ``A`` and would slip past a bounded pattern. Over-matching
#: inside a longer token is the fail-closed direction for a redaction floor.
AWS_KEY_ID_REDACTION = (
    f"(?:{AWS_KEY_ID_PREFIXES}|{AWS_KEY_ID_REDACTION_ONLY_PREFIXES}){AWS_KEY_ID_BODY}"
)

#: Multi-segment JWT: an ``eyJ`` header plus 2-4 further dot-separated base64url
#: segments, covering 3-segment JWS AND 4-5-segment JWE (``dir``/ECDH-ES).
#: Catches tokens carried OUTSIDE a ``Bearer `` scheme -- JSON-embedded, bare, or
#: assignment-style.
#:
#: The segment class cannot cross the literal ``.`` separators, so it does not
#: over-capture into surrounding text. ``security.py`` carries a SECOND,
#: deliberately different two-segment link-token pattern whose ``{96,}`` floor is
#: tuned against false positives such as ``eyJ2IjoxfQ.json``; that one is a
#: distinct pattern for a distinct job, not another spelling of this one, and it
#: stays where it is used.
JWT_MULTI_SEGMENT = r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}"

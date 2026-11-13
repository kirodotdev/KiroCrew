"""The exfil base64-blob test: the git object name carve-out and joined query values.

A commit permalink can carry a SHA-1 git object name in its query, and that name is a
40+ character run of the base64 class in ``_EXFIL_PATTERNS``. This module holds the
shape of that name and the blob test that skips it. It also holds the checks that
judge a query's parameter values joined, so a secret cut into several short values
is seen as one run. The URL classifier and the OAuth credential diagnosis in
:mod:`kiro_crew.security.exfil` call these tests; this module never imports that
facade.
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus

from .exfil_url_rules import _EXFIL_PATTERNS, _HARD_CREDENTIAL_RE, _MAX_URL_DECODE_PASSES
from .redaction import _contains_fixed_credential

# A SHA-1 git object name -- 40 lowercase hex digits -- is a 40+ char run of the
# base64 class in ``_EXFIL_PATTERNS``, so a commit permalink that carries one in
# its QUERY (``?commit=<sha>``, ``?changes=pkg/main:<sha>``) trips the blob
# branch. It is not a base64 blob: hex is a 16-symbol alphabet, the reasoning
# ``redaction.py``'s bare-secret detector already applies to all-hex runs. The
# carve-out is that exact length, lowercase as git prints it, judged on the
# MAXIMAL match: 41+ digits, mixed case, ``=`` padding, or a hash glued to any
# other base64 character is a longer or wider run and keeps firing. 64 hex
# (SHA-256) is NOT carved out: it is the shape of this product's own secrets
# (``token_hex(32)``, e.g. ``generate_app_secret()``). Residual, stated: a 40-hex
# token in a query passes THIS heuristic; the fixed credential markers key on
# provider prefixes, and the length gate still bounds the query.
_GIT_OBJECT_NAME_RE = re.compile(r"[0-9a-f]{40}")


def _exfil_blob_hit(value: str) -> bool:
    """True when ``_EXFIL_PATTERNS`` matches ``value`` on anything but a bare git
    object name. Every other alternative (AWS key id, SSH/PEM header, Slack
    token, heavy percent encoding) is reported unchanged."""
    for match in _EXFIL_PATTERNS.finditer(value):
        if _GIT_OBJECT_NAME_RE.fullmatch(match.group(0)):
            continue
        return True
    return False


# Query values judged JOINED. The per-URL checks look for a pattern inside the
# query text, where ``&``, ``=`` and the parameter names break every run, so a
# secret cut into several short values (each under the 40-char run, the whole
# query under ``_EXFIL_QUERY_MIN_LEN``) matched nothing. These rebuild the values
# in order with the separators removed and judge that string too. Mirrored by
# ``website/src/utils/sanitize.ts`` (``joinedCredentialHit`` / ``joinedBlobHit``).
#
# A piece made only of the base64 class (``=`` already split it off).
_B64_QUERY_PIECE_RE = re.compile(r"[A-Za-z0-9+/]*")
# The shortest piece the blob join counts: shorter ones are ordinary ids and words.
_JOINED_B64_MIN_PIECE = 16


def _query_values(query: str) -> list[str]:
    """The value of every ``&``-separated parameter, in order ("" when bare)."""
    return [segment.partition("=")[2] for segment in query.split("&")]


def _joined_values_credential_hit(query: str) -> bool:
    """True when the joined values carry a hard or canonical credential.

    Unconditional, like the same checks on the whole URL: every value joins,
    whatever its alphabet, because the markers include ``-`` and ``_``. A ``=``
    inside a value is judged both kept (``key=value`` markers) and removed (a key
    id cut at a ``=``). The join is decoded as often as the URL payload is.
    """
    joined = "".join(_query_values(query))
    for _ in range(_MAX_URL_DECODE_PASSES + 1):
        for form in (joined, joined.replace("=", "")):
            if _HARD_CREDENTIAL_RE.search(form) or _contains_fixed_credential(form):
                return True
        decoded = unquote_plus(joined)
        if decoded == joined:
            break
        joined = decoded
    return False


def _joined_b64_values(values: list[str]) -> str:
    """Join, in order, the long pieces of the values made wholly of the base64 class.

    Each value is form-decoded first, so ``+`` is a space and search words
    (``q=how+to+bake``) never join. It is then cut at each ``=``, so
    ``a=<chunk>=<chunk>`` joins like ``a=<chunk>&b=<chunk>``. A piece joins only
    when it is wholly base64-class and at least ``_JOINED_B64_MIN_PIECE`` chars.
    A piece holding any other byte (``-``, ``_``, ``.``, ``:``) cannot sit inside
    one base64 run on its own, and short ids and words (a video id, ``index=2``)
    are what ordinary links carry, so ``watch?v=<id>&list=PL<id>`` is not one run.
    A piece that does not qualify is SKIPPED, not treated as a break, so a filler
    parameter between two chunks does not split the secret. A bare git object
    name is skipped too: it is carved out above, and gluing it to a neighbour
    (``?commit=<sha>&tab=files``) would undo that. Parameter names are not joined.
    shortcut: a secret cut into pieces under 16 chars is bounded only by
    ``_EXFIL_QUERY_MIN_LEN``.
    """
    return "".join(
        piece
        for value in values
        for piece in unquote_plus(value).split("=")
        if len(piece) >= _JOINED_B64_MIN_PIECE
        and _B64_QUERY_PIECE_RE.fullmatch(piece)
        and not _GIT_OBJECT_NAME_RE.fullmatch(piece)
    )


def _joined_values_blob_hit(heuristic_query: str) -> bool:
    """True when the joined long base64-class values form a base64 run.

    Same waivable tier as the single-value blob check: the caller passes the
    payload it already chose for that check, after its host and shape
    exemptions, so nothing those exemptions skip is judged here.
    """
    return _exfil_blob_hit(_joined_b64_values(_query_values(heuristic_query)))

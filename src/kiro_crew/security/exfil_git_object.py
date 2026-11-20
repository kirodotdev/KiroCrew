"""The carve-outs from the exfil base64-blob heuristic.

A commit permalink can carry a SHA-1 git object name in its query, and that name is a
40+ character run of the base64 class in ``_EXFIL_PATTERNS``. A search query that
joins plain words or ids with ``+`` (a form-encoded space) is such a run too. This
module holds those two shapes and the blob test that skips them. The URL classifier
and the OAuth credential diagnosis in :mod:`kiro_crew.security.exfil` call the test;
this module never imports that facade.
"""

from __future__ import annotations

import re

from .exfil_url_rules import _EXFIL_PATTERNS

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

# One word of a ``+``-joined query (``T1000000001+OR+T1000000002``,
# ``how+to+set+a+timezone``): letters in ONE case (or a capital then lowercase),
# then digits. In a query ``+`` is a space, and the same words joined with ``%20``
# already pass, because the decoded copy has spaces between them. A base64 secret
# split at its own ``+`` characters is not words: its pieces mix case inside a run
# of letters, or put a letter after a digit, or hold ``/``, and any such piece keeps
# the whole run firing. Each piece is also scanned on its own, so an AWS key id
# joined into the list still fires through the pattern's own alternative.
_QUERY_WORD_RE = re.compile(r"(?:[A-Z]{0,24}|[A-Z]?[a-z]{0,24})[0-9]{0,24}")


def _is_plus_joined_words(run: str) -> bool:
    """Whether *run* is two or more plain words joined by ``+``, none a blob itself."""
    pieces = run.split("+")
    if len(pieces) < 2:
        return False
    return all(
        piece and _QUERY_WORD_RE.fullmatch(piece) and not _EXFIL_PATTERNS.search(piece)
        for piece in pieces
    )


def _exfil_blob_hit(value: str) -> bool:
    """True when ``_EXFIL_PATTERNS`` matches ``value`` on anything but a bare git
    object name or a ``+``-joined list of plain words. Every other alternative (AWS
    key id, SSH/PEM header, Slack token, heavy percent encoding) is reported
    unchanged."""
    for match in _EXFIL_PATTERNS.finditer(value):
        run = match.group(0)
        if _GIT_OBJECT_NAME_RE.fullmatch(run) or _is_plus_joined_words(run):
            continue
        return True
    return False

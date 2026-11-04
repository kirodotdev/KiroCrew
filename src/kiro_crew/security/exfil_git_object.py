"""The git object name carve-out from the exfil base64-blob heuristic.

A commit permalink can carry a SHA-1 git object name in its query, and that name is a
40+ character run of the base64 class in ``_EXFIL_PATTERNS``. This module holds the
shape of that name and the blob test that skips it. The URL classifier and the OAuth
credential diagnosis in :mod:`kiro_crew.security.exfil` call the test; this module
never imports that facade.
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


def _exfil_blob_hit(value: str) -> bool:
    """True when ``_EXFIL_PATTERNS`` matches ``value`` on anything but a bare git
    object name. Every other alternative (AWS key id, SSH/PEM header, Slack
    token, heavy percent encoding) is reported unchanged."""
    for match in _EXFIL_PATTERNS.finditer(value):
        if _GIT_OBJECT_NAME_RE.fullmatch(match.group(0)):
            continue
        return True
    return False

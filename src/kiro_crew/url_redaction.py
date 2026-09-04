"""The one redactor for a model URL that is about to be written down.

A model URL is operator-supplied — a mirror, an artifact repository, a pre-signed
object — and every one of those can carry a bearer credential. It reaches a human
only on a failure path (a rejected setting, a refused redirect, a download that
did not work), which is exactly where a bare ``%r`` published it verbatim into the
gateway log, the dashboard's ``/api/logs`` stream, and a status field a settings
page reads back.

This module exists so that reduction has ONE implementation. It was written four
times instead: in :mod:`kiro_crew.embeddings`, in papyrus's Tectonic download, in
pptx-maker's engine download, and once more for the whisper catalog. The copies
then disagreed — two of them kept the PATH and two dropped it — and a mirror that
tokenises its path (``/artifactory/api/npm/tok-9f3b2c/…``) had its token logged by
the copies that kept it. Host-only is the surviving policy, and it lives here.

Nothing but ``urllib.parse`` is imported, deliberately: the callers are a config
validator, a download path and a diagnostics command, which sit on either side of
:mod:`kiro_crew.config`, so anything heavier here would be a cycle.
"""

from __future__ import annotations

import urllib.parse

#: What a value that cannot be taken apart is reduced to. No fragment of the input
#: survives: with no authority to split off, every character sits in ``path`` — a
#: scheme-less ``user:token@host/x`` included — so there is nothing here that is
#: safe to emit.
UNPARSEABLE_URL = "<unparseable URL>"


def redact_model_url(value: object) -> str:
    """Reduce *value* to ``scheme://host[:port]``, safe for a log or a UI field.

    A credential rides in a URL in four places, and all four are dropped:
    ``userinfo`` (``https://user:token@host/…``), the QUERY and the FRAGMENT (where
    a pre-signed signature lives), and a PATH SEGMENT (a path-tokenised mirror, or
    a presigned-style ``/AKIA…/…``). Dropping the path costs the one diagnostic it
    carried — the artifact's filename — which is recoverable from the pinned
    constants and from the digest in the message beside it; host-only is what makes
    this safe by CONSTRUCTION rather than by enumerating credential shapes.

    Rebuilt from parsed components, never pattern-stripped: a regex over a string
    the far end chose decides nothing, while ``urlsplit`` already knows where the
    authority ends.

    Never raises, and never emits any part of a value it could not take apart. Every
    caller is already on a failure path, and a redactor that raises there replaces a
    deliberate refusal with a traceback. ``value`` is typed ``object`` because one
    caller is a configuration validator, where it is whatever JSON held — a
    non-string is described by its type and nothing else.
    """
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    try:
        parts = urllib.parse.urlsplit(value.strip())
        host = parts.hostname or ""
        # Read inside the guard: both attributes parse the authority lazily, so an
        # unclosed IPv6 literal or a non-numeric port raises HERE, not above.
        port = parts.port
    except ValueError:
        return UNPARSEABLE_URL
    if not parts.scheme or not host:
        return UNPARSEABLE_URL
    authority = f"{host}:{port}" if port is not None else host
    return f"{parts.scheme}://{authority}"

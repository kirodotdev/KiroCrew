"""The one redactor for a model URL that is about to be written down.

A model URL is operator-supplied -- a mirror, an artifact repository, a pre-signed
object -- and every one of those can carry a bearer credential. It reaches a human
only on a failure path (a rejected setting, a refused redirect, a download that
did not work), which is exactly where a bare ``%r`` published it verbatim into the
gateway log, the dashboard's ``/api/logs`` stream, and a status field a settings
page reads back.

**The reduction itself is not reimplemented here.**
:func:`kiro_crew.asset_downloader.redact_url` already rebuilds a URL as
``scheme://host[:port]``, for the same reason and with the same finding about the
path being the component that looks safe and is not -- so this module calls it
rather than becoming another copy of it. What it adds is the strictness a FAILURE
path needs and a bulk transfer does not, and nothing else:

* a value that is not a string at all, because one caller is a configuration
  validator holding whatever the JSON held;
* a value with no authority to keep, where the shared function answers with an
  empty string or a scheme with nothing after it -- neither is safe to hand to a
  caller that will interpolate it into a message;
* a host that cannot be PRINTED, which matters because two of these callers write
  to an operator's terminal.

Reducing four hand-written copies to one was the point, and the count was wrong in
both directions: the shared implementation above already existed, and two copies
survive this change, at ``apps/builtins/papyrus/backend/tectonic.py`` and
``apps/builtins/pptx_maker/backend/engine_source.py``. Migrating those two is a
mechanical import swap and a separate change -- each is a live download path with
its own tests. The copy in :mod:`kiro_crew.embeddings` is deleted by this one.
"""

from __future__ import annotations

from kiro_crew.asset_downloader import redact_url

#: What a value that cannot be taken apart is reduced to. No fragment of the input
#: survives: with no authority to split off, every character sits in ``path`` -- a
#: scheme-less ``user:token@host/x`` included -- so there is nothing here that is
#: safe to emit.
UNPARSEABLE_URL = "<unparseable URL>"

#: What the shared reduction answers when it could not split the value at all. Its
#: spelling is its own; a caller must not publish it as if it were a host.
_SHARED_UNPARSEABLE = "<unparseable-url>"


def redact_model_url(value: object) -> str:
    """Reduce *value* to ``scheme://host[:port]``, safe for a log or a UI field.

    A credential rides in a URL in four places, and the shared reduction drops all
    four: ``userinfo`` (``https://user:token@host/...``), the QUERY and the FRAGMENT
    (where a pre-signed signature lives), and a PATH SEGMENT (a path-tokenised
    mirror, or a presigned-style ``/AKIA.../...``). Dropping the path costs the one
    diagnostic it carried -- the artifact's filename -- which is recoverable from
    the pinned constants and from the digest in the message beside it; host-only is
    what makes this safe by CONSTRUCTION rather than by enumerating credential
    shapes.

    Never raises, and never emits any part of a value it could not take apart. Every
    caller is already on a failure path, and a redactor that raises there replaces a
    deliberate refusal with a traceback. ``value`` is typed ``object`` because of the
    configuration validator; a non-string is described by its type and nothing else.
    """
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    reduced = redact_url(value.strip())
    # Three answers that are not a host: the shared sentinel, an empty string (no
    # authority survived), and a scheme with no authority after it (``file:``).
    if not reduced or reduced == _SHARED_UNPARSEABLE or "://" not in reduced:
        return UNPARSEABLE_URL
    # ``urlsplit`` removes only tab, CR and LF, so any OTHER control byte the far
    # end chose survives in the host -- and this value is written to an operator's
    # terminal on a failure path. An ESC resets or repaints that terminal, a BEL
    # rings it, and a bidi override (U+202E) reverses the host it appears to name;
    # none of it is recoverable once emitted. ``isprintable()`` is False for exactly
    # those classes (Cc, Cf, Zl, Zp, and space, which no host may contain either),
    # so a host we cannot print is a host we do not name: the whole value reduces.
    if not reduced.isprintable():
        return UNPARSEABLE_URL
    return reduced

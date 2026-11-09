"""Plain readable text from Slack mrkdwn, for a session's title.

A conversation started in Slack is stored as Slack sent it: ``<https://…|label>``
links, ``<#C123|name>`` channel refs, ``<@U123>`` mentions, ``<!here>`` and
``&amp;`` / ``&lt;`` / ``&gt;`` escapes. Its first message becomes the session's
title when nothing better names it, so without this the sidebar shows
``how is <https://github.com/kirodotdev/KiroCrew/issues/21964|gi…``.

:func:`slack_title_text` reads each construct as a person reads it in Slack:

* ``<url|label>`` -> ``label``. Slack's own auto-link writes the URL itself as
  the label (``<https://github.com/o/r/issues/1|github.com/o/r/issues/1>``), and
  that label, or an empty one, reads like a bare ``<url>``.
* bare ``<url>`` -> a short form: a GitHub issue or pull request becomes
  ``owner/repo#N``; anything else is the host plus a short path.
* ``<#C123|name>`` -> ``#name`` (``#channel`` with no name).
* ``<@U123|name>`` -> ``@name``; a bare ``<@U123>`` -> ``@someone``. A title is
  derived where no Slack client is at hand, so an id is never looked up here,
  and a raw id is never shown either.
* ``<!here>`` / ``<!channel>`` / ``<!everyone>`` -> ``@here`` / ``@channel`` /
  ``@everyone``; a user group or date -> its label.
* the three entity escapes are unescaped, after the brackets are read, so an
  escaped ``&lt;…&gt;`` the person typed as text stays text.

A construct cut open by an earlier truncation (a title persisted as the first 80
characters of the raw message) is read the same way, so already-stored titles
clean up too. Anything else in angle brackets is left as written.

IDEMPOTENT: a converted title is shown, saved back as the session's title and
listed (converted) again, so converting the output must change nothing. An
escape is therefore decoded only when the decoded text cannot itself be read as
a construct or an escape on the next pass: ``&lt;!here&gt;`` that a person typed
stays escaped rather than becoming ``<!here>`` and then ``@here``.

Standard library only at import time: the session catalog imports it.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

__all__ = [
    "SLACK_TEXT_META_KEY",
    "is_slack_session_key",
    "is_slack_slot",
    "is_slack_text_row",
    "slack_title_text",
    "stored_title_is_mrkdwn",
]

#: Transcript row meta: this user row's text is Slack mrkdwn, copied from a Slack
#: thread into a dashboard tab (``!link-to-dashboard``). Host-written only.
SLACK_TEXT_META_KEY = "slackText"

#: Characters a ``<…>`` construct's body may not hold: it is one line, unnested.
_NOT_IN_BODY = "<>\n"
#: URL schemes Slack links, compared case-insensitively.
_URL_SCHEMES = ("http://", "https://", "ftp://")
#: A URL path's separator: always "/", on every OS (not a filesystem path).
_URL_SEP = "/"
#: GitHub path kinds that name an issue or a pull request.
_GITHUB_REF_KINDS = ("issues", "pull", "pulls")
_BROADCASTS = frozenset({"here", "channel", "everyone"})
#: Path segments kept in the short form of a non-GitHub URL.
_SHORT_PATH_SEGMENTS = 2
#: A cut-off label shorter than this that has no dot or slash is the person's
#: own word ("help"), not the start of Slack's URL-as-label.
_ECHO_MIN_CHARS = 8
#: Passes before a title is taken as settled. One pass settles everything Slack
#: sends; unclosed ``<`` text typed by hand (a rename) can take another.
_MAX_PASSES = 4
#: The three escapes Slack applies. ``&amp;`` that would decode into the start of
#: another escape is kept, so a decoded ``&`` never begins one on the next pass.
_DECODED = {"&lt;": "<", "&gt;": ">", "&amp;": "&"}
#: What follows ``&`` in an escape, the tails an ``&amp;`` guard looks for.
_ENTITY_TAILS = ("lt;", "gt;", "amp;")


def _has_url_scheme(url: str) -> bool:
    return url.lower().startswith(_URL_SCHEMES)


def _without_scheme(url: str) -> str:
    low = url.lower()
    for scheme in _URL_SCHEMES:
        if low.startswith(scheme):
            return url[len(scheme) :]
    return url


def _github_ref(path: str) -> str | None:
    """``owner/repo#N`` for ``/owner/repo/(issues|pull|pulls)/N[/…]``, else None."""
    segs = path.split(_URL_SEP)
    if len(segs) < 5 or segs[0] or not segs[1] or not segs[2]:
        return None
    if segs[3] not in _GITHUB_REF_KINDS or not segs[4].isdecimal():
        return None
    return f"{segs[1]}/{segs[2]}#{segs[4]}"


def _closed_constructs(text: str) -> Iterator[tuple[int, int, str]]:
    """``(start, end, body)`` of each closed ``<body>``, left to right, unnested."""
    i, n = 0, len(text)
    while (start := text.find("<", i)) >= 0:
        j = start + 1
        while j < n and text[j] not in _NOT_IN_BODY:
            j += 1
        if j < n and text[j] == ">":
            yield start, j + 1, text[start + 1 : j]
            i = j + 1
        else:
            i = start + 1


def _open_tail(text: str) -> int:
    """Where a ``<`` that a cut left open at the very end starts, or -1."""
    start = text.rfind("<")
    if start < 0 or any(c in text[start + 1 :] for c in _NOT_IN_BODY):
        return -1
    return start


def _entities(text: str) -> Iterator[tuple[int, str]]:
    """``(start, escape)`` of each ``&lt;``/``&gt;``/``&amp;``, left to right."""
    i = 0
    while (start := text.find("&", i)) >= 0:
        for escape in _DECODED:
            if text.startswith(escape, start):
                yield start, escape
                i = start + len(escape)
                break
        else:
            i = start + 1


def is_slack_session_key(key: str) -> bool:
    """True for a Slack conversation's key: ``slack:<ts>``, its ``slack_`` file stem, or a bare ts."""
    from kiro_crew.constants import SLACK_NAMESPACE

    if key.startswith((f"{SLACK_NAMESPACE}:", f"{SLACK_NAMESPACE}_")):
        return True
    # Lazy: the messaging package pulls in channel code this module does not
    # otherwise need. The history modules that call this already import it.
    from kiro_crew.messaging.link import is_legacy_slack_key

    return is_legacy_slack_key(key)


def is_slack_slot(slot: object) -> bool:
    """Whether *slot* is a Slack conversation's tab, so its user text is Slack mrkdwn."""
    return is_slack_session_key(getattr(slot, "key", ""))


#: Stored ``title_origin`` values that mean a person typed the title (a rename),
#: so it is their own words, not Slack mrkdwn.
_TYPED_TITLE_ORIGINS = frozenset({"user", "member"})


def stored_title_is_mrkdwn(title: object, origin: object) -> bool:
    """Whether a Slack session's persisted *title* is read as mrkdwn.

    Only a string, and only when its stored ``title_origin`` is not a person's
    rename: a dashboard rename to ``Debug &amp; decoding`` is kept as typed, and a
    malformed non-string title is left for the caller's own fallback. A
    non-string ``title_origin`` (an unhashable ``[]`` or ``{}``) is unknown
    provenance, read like a missing one, and never raises.
    """
    if not isinstance(title, str):
        return False
    return not (isinstance(origin, str) and origin in _TYPED_TITLE_ORIGINS)


def is_slack_text_row(row: object) -> bool:
    """Whether transcript *row* was copied in from a Slack thread (``SLACK_TEXT_META_KEY``)."""
    meta = row.get("meta") if isinstance(row, dict) else None
    return isinstance(meta, dict) and meta.get(SLACK_TEXT_META_KEY) is True


def _short_url(url: str, *, cut: bool = False) -> str:
    """``owner/repo#N`` for a GitHub issue or PR, else host plus a short path."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
    except ValueError:
        return url
    if not host:
        return url
    if host == "github.com" and not cut:
        ref = _github_ref(parts.path)
        if ref:
            return ref
    segments = [s for s in parts.path.split(_URL_SEP) if s]
    short = _URL_SEP.join([host, *segments[:_SHORT_PATH_SEGMENTS]])
    if cut or len(segments) > _SHORT_PATH_SEGMENTS:
        short += "/…" if segments else "…"
    return short


def _label_echoes_url(label: str, url: str, *, cut: bool) -> bool:
    """Whether *label* is the URL itself (Slack's auto-link): whole, or cut off when *cut*."""
    if not label:
        return True
    bare = _without_scheme(url)
    if cut:
        # A title an earlier build cut already ends its label with "…".
        label = label.removesuffix("…")
        if len(label) < _ECHO_MIN_CHARS and not any(c in label for c in "./"):
            return False
        return url.startswith(label) or bare.startswith(label)
    return label in (url, bare, bare.rstrip("/"))


def _read(body: str, *, cut: bool = False) -> str | None:
    """How a person reads the ``<body>`` construct, or None when it is not one."""
    target, has_label, label = body.partition("|")
    if target.startswith(("#C", "#G")):
        return f"#{label}" if label else "#channel"
    if target.startswith(("@U", "@W", "@B")):
        return f"@{label.lstrip('@')}" if label else "@someone"
    if target.startswith("!"):
        name = target[1:]
        if name in _BROADCASTS:
            return f"@{name}"
        if name.startswith("subteam^"):
            return label or "@team"
        if name.startswith("date^"):
            return label or None
        return None
    if target.lower().startswith("mailto:"):
        return label or target[len("mailto:") :]
    if _has_url_scheme(target):
        if has_label and not _label_echoes_url(label, target, cut=cut):
            return f"{label.removesuffix('…')}…" if cut else label
        # Cut inside the target itself (no label yet): the URL is partial.
        return _short_url(target, cut=cut and not has_label)
    return None


def _read_constructs(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end, body in _closed_constructs(text):
        read = _read(body)
        pieces.append(text[cursor:start])
        pieces.append(text[start:end] if read is None else read)
        cursor = end
    pieces.append(text[cursor:])
    out = "".join(pieces)
    tail = _open_tail(out)
    if tail >= 0:
        read = _read(out[tail + 1 :], cut=True)
        if read is not None:
            out = out[:tail] + read
    return out


def _decode(text: str, *, angles: bool) -> str:
    """Decode the escapes; ``&lt;``/``&gt;`` only when *angles*, guarded ``&amp;`` never."""
    pieces: list[str] = []
    cursor = 0
    for start, entity in _entities(text):
        keep = (entity != "&amp;" and not angles) or (
            entity == "&amp;" and text.startswith(_ENTITY_TAILS, start + len("&amp;"))
        )
        pieces.append(text[cursor:start])
        pieces.append(entity if keep else _DECODED[entity])
        cursor = start + len(entity)
    pieces.append(text[cursor:])
    return "".join(pieces)


def slack_title_text(text: str) -> str:
    """*text* as plain readable title text: every Slack construct read, whitespace folded.

    Run to a fixed point, so the result is one more pass would leave alone.
    """
    out = text
    for _ in range(_MAX_PASSES):
        nxt = _one_pass(out)
        if nxt == out:
            break
        out = nxt
    return out


def _one_pass(text: str) -> str:
    if not text:
        return text
    # Folded first: a construct is read within one line, so a fold done after
    # could join a ``<`` and ``>`` that were lines apart into one next time.
    out = _read_constructs(" ".join(text.split()))
    decoded = _decode(out, angles=True)
    if _read_constructs(decoded) != decoded:
        # A decoded bracket would be read as a construct next time: keep it escaped.
        decoded = _decode(out, angles=False)
    return " ".join(decoded.split())

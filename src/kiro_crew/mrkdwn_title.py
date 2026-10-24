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

Standard library only: the session catalog imports it.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

__all__ = ["SLACK_TEXT_META_KEY", "is_slack_session_key", "slack_title_text"]

#: Transcript row meta: this user row's text is Slack mrkdwn, copied from a Slack
#: thread into a dashboard tab (``!link-to-dashboard``). Host-written only.
SLACK_TEXT_META_KEY = "slackText"

#: A closed ``<…>`` construct, and one a truncation cut open at the very end.
_CLOSED = re.compile(r"<([^<>\n]*)>")
_OPEN_TAIL = re.compile(r"<([^<>\n]*)$")
_URL_SCHEME = re.compile(r"^(?:https?|ftp)://", re.IGNORECASE)
_GITHUB_REF = re.compile(r"^/([^/]+)/([^/]+)/(?:issues|pull|pulls)/(\d+)(?:[/?#].*)?$")
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
_ENTITY = re.compile(r"&(?:lt|gt|amp);")
_AMP_GUARD = re.compile(r"&amp;(?=(?:lt|gt|amp);)")
_DECODED = {"&lt;": "<", "&gt;": ">", "&amp;": "&"}
#: A legacy Slack transcript is named by its bare thread ts. Same shape as
#: ``messaging.link.is_legacy_slack_key``, which this module cannot import (the
#: messaging package pulls in the channel driver).
_LEGACY_SLACK_KEY = re.compile(r"\d{1,20}\.\d{1,20}")


def is_slack_session_key(key: str) -> bool:
    """True for a Slack conversation's key: ``slack:<ts>``, its ``slack_`` file stem, or a bare ts."""
    return key.startswith(("slack:", "slack_")) or bool(_LEGACY_SLACK_KEY.fullmatch(key))


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
        ref = _GITHUB_REF.match(parts.path)
        if ref:
            return f"{ref.group(1)}/{ref.group(2)}#{ref.group(3)}"
    segments = [s for s in parts.path.split("/") if s]
    short = "/".join([host, *segments[:_SHORT_PATH_SEGMENTS]])
    if cut or len(segments) > _SHORT_PATH_SEGMENTS:
        short += "/…" if segments else "…"
    return short


def _label_echoes_url(label: str, url: str, *, cut: bool) -> bool:
    """Whether *label* is the URL itself (Slack's auto-link): whole, or cut off when *cut*."""
    if not label:
        return True
    bare = _URL_SCHEME.sub("", url)
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
    if _URL_SCHEME.match(target):
        if has_label and not _label_echoes_url(label, target, cut=cut):
            return f"{label.removesuffix('…')}…" if cut else label
        # Cut inside the target itself (no label yet): the URL is partial.
        return _short_url(target, cut=cut and not has_label)
    return None


def _read_constructs(text: str) -> str:
    def _closed(m: re.Match[str]) -> str:
        read = _read(m.group(1))
        return m.group(0) if read is None else read

    out = _CLOSED.sub(_closed, text)
    tail = _OPEN_TAIL.search(out)
    if tail:
        read = _read(tail.group(1), cut=True)
        if read is not None:
            out = out[: tail.start()] + read
    return out


def _decode(text: str, *, angles: bool) -> str:
    """Decode the escapes; ``&lt;``/``&gt;`` only when *angles*, guarded ``&amp;`` never."""
    pieces: list[str] = []
    cursor = 0
    for m in _ENTITY.finditer(text):
        entity = m.group(0)
        keep = (entity != "&amp;" and not angles) or (
            entity == "&amp;" and _AMP_GUARD.match(text, m.start()) is not None
        )
        pieces.append(text[cursor : m.start()])
        pieces.append(entity if keep else _DECODED[entity])
        cursor = m.end()
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

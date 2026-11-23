"""The backend's alternation placeholder, as the model repeats it in a reply.

A turn that ends on a tool result (a quiet end, a tool-only turn, an interrupted
turn) leaves the backend's history with no assistant message after it. On the
next request the backend fills that gap with a self-describing assistant
placeholder, "(No assistant response was recorded for the preceding turn. ...)",
so the roles still alternate. The model then reads it as its own earlier reply
and opens a later turn by writing it again. That text is not a reply: it must
never become a transcript row.

The live stream holds a segment while it could still be this text and drops it
when it is: the dashboard runner (``stream_chunks._on_text_chunk``), the channel
turn driver (``messaging.driver.TurnDriver``), the shared collector
(``llm_helpers.stream_and_collect``) and the native Slack turn
(``slack/handler_runtime/stream.py``) alike. A turn whose only text was the
placeholder then gets the usual empty-reply handling. Rows an older build already
stored with only that text are left as they are. The model's own history is
untouched: the backend keeps its placeholder.
"""

from __future__ import annotations

import re

#: How every spelling of the placeholder opens, the backend's long form and the
#: short form a model repeats alike.
OPENER = "(No assistant response was recorded"

#: The whole placeholder: the opener, then text with no parenthesis, then ``)``.
_ECHO_RE = re.compile(r"\s*" + re.escape(OPENER) + r"[^()]*\)")

#: Past this many characters an unclosed opener is not the placeholder (its long
#: form is under 200), so a held segment that grows longer is released.
_MAX_ECHO_CHARS = 400


def echo_prefix_len(text: str) -> int:
    """Length of the placeholder that *text* starts with, or 0 when it does not."""
    m = _ECHO_RE.match(text)
    return m.end() if m else 0


def is_echo(text: str) -> bool:
    """Whether *text* is the placeholder and nothing else but whitespace."""
    n = echo_prefix_len(text)
    return bool(n) and not text[n:].strip()


def could_be_echo(text: str) -> bool:
    """Whether *text* is the placeholder, or the start of it as it streams in."""
    if len(text) > _MAX_ECHO_CHARS:
        return False
    if is_echo(text):
        return True
    t = text.lstrip()
    if len(t) <= len(OPENER):
        return OPENER.startswith(t)
    if not t.startswith(OPENER):
        return False
    return "(" not in t[len(OPENER) :] and ")" not in t[len(OPENER) :]

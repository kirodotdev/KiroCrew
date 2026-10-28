"""How ``SlackRenderer`` cuts the turn's text: into steps' narration and reply pieces.

The import path callers use is :mod:`kiro_crew.slack.renderer`, which re-exports
every name here.
"""

from __future__ import annotations

import re

from kiro_crew.dashboard.chat_utils_turn_text import open_construct_at_end

#: Characters held back below ``max_message_chars`` when splitting. The shared
#: splitter may exceed its limit by the fence scaffolding of a whole-line
#: placement; this absorbs the ordinary case so no chunk reaches
#: :data:`SLACK_MSG_LIMIT`, where ``_render_fallback`` truncates it.
_SPLIT_HEADROOM = 100

#: Appended to a partially-streamed assistant row that the dispatcher rescues when
#: a turn dies mid-flight. Without it the retry reads a reply that simply stops
#: mid-sentence and cannot tell a truncated turn from a finished one — so it may
#: treat the work as already reported and answer nothing. The wording addresses
#: the next turn directly, because that turn is the only consumer.
#:
#: It lives in the Slack package rather than in the shared ``messaging`` layer because the rescue
#: it belongs to is Slack-only, and for the reason in ``delivered_text``: Slack is
#: the one renderer that records WHICH appended bytes the API acknowledged, so it
#: is the one channel where a turn that dies mid-flight can persist text the user
#: provably saw. Other renderers on the shared path do emit mid-turn; none of them
#: can say afterwards what the user retained.
PARTIAL_TURN_MARKER = (
    "\n\n_[This turn was cut off here by a transport/backend failure, not finished. "
    "Everything above was already established — continue from this point instead of "
    "starting the request over.]_"
)

#: Largest piece of the reply sent in one ``chat.appendStream``. Slack caps a
#: ``markdown_text`` chunk at 12,000 characters; the margin leaves room for the
#: redaction placeholders ``_append_stream`` may lengthen a piece by.
_STREAM_APPEND_MAX = 10_000

#: Longest narration a step's details carry. Narration is a sentence or two
#: before a tool call; the bound keeps a long one from flooding the plan.
_NARRATION_MAX_CHARS = 1_500

_THINKING_OPEN_RE = re.compile(r"<(?:antml:)?thinking>")
_THINKING_CLOSE_RE = re.compile(r"</(?:antml:)?thinking>")


def _reply_construct_open(text: str) -> bool:
    """Whether *text* ends inside a construct that only renders whole.

    An ``[OPTIONS:`` line, a code fence or a widget still open
    (``open_construct_at_end``, the dashboard's own segment-cut rule), or a
    ``<thinking>`` block with no close yet. A tool call landing there (a
    background child's call interleaves with the parent's stream) must not cut
    the text: the halves would be sealed apart and neither would parse.
    """
    if open_construct_at_end(text):
        return True
    opens = [m.start() for m in _THINKING_OPEN_RE.finditer(text)]
    closes = [m.start() for m in _THINKING_CLOSE_RE.finditer(text)]
    return bool(opens) and (not closes or opens[-1] > closes[-1])


def _append_pieces(text: str, limit: int = _STREAM_APPEND_MAX) -> list[str]:
    """Cut *text* into pieces of at most *limit* characters, in order.

    Each cut lands after the last line break in reach, else after the last
    space, so a piece never ends inside a word; only a run with neither is cut
    where the limit falls. Joined, the pieces are *text* exactly.
    """
    pieces: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = window.rfind("\n") + 1 or window.rfind(" ") + 1 or limit
        pieces.append(text[:cut])
        text = text[cut:]
    if text:
        pieces.append(text)
    return pieces

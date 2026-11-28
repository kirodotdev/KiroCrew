"""When one Slack streaming message is closed and a fresh one opened below it.

Slack closes a stream 10 minutes after ``chat.startStream``, however often it is
appended to: the limit is on the stream's total life, not on idle time. At that
point Slack finalizes the message itself and turns every task card still
``pending`` or ``in_progress`` into ``error``, which the reader sees as a failed
step ("Something went wrong") on a turn that is still running. The gateway is not
told; the next append just returns ``message_not_in_streaming_state``.

So both Slack streaming paths stop the stream and start a fresh one in the same
thread before that limit, at the first append after ``STREAM_RENEW_AFTER_S``. The
margin covers the 30 s task-card refresh, which is the only append during a long
tool phase.

A stream also stays where it was opened, so a message a person sends while the
turn runs lands below it, and the thread then ends on that message with the
turn's steps still moving above it: it reads as not picked up. Every streaming
site therefore draws its next running step in a fresh stream once such a message
has landed (``note_thread_message`` / ``message_landed_since``). Messages are
counted, not timed: a site keeps the count (``messages_noted``) it read before
it asked Slack for the stream, so a message that lands while the stream is being
opened counts as below it, and no clock's resolution decides the order. The
messages recorded are the ones the route takes in, an admitted trusted bot's
among them: it sits below the steps like a person's.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

#: Slack's hard limit on one stream's life, in seconds.
SLACK_STREAM_LIFETIME_S = 600.0

#: Renew a stream at its first append after this age, in seconds.
STREAM_RENEW_AFTER_S = 540.0

#: Appended to a running step's title on the message being closed, so the step
#: does not read as stuck there; the step goes on in the next message.
CONTINUES_BELOW = " · continues below"

#: Threads whose latest message is remembered; the oldest is forgotten first. A
#: forgotten thread only keeps its stream in place until its next message.
MAX_NOTED_THREADS = 1024

# How many messages the route has recorded, and for each thread the count its
# latest one took.
_noted = 0
_thread_message_no: OrderedDict[tuple[str, str], int] = OrderedDict()


def stream_clock() -> float:
    """The running event loop's clock, which a stream's age is measured on.

    The loop's own clock rather than ``time.monotonic``: the handlers' turn clock
    times footers and edit throttles, and tests script it read by read, so an age
    check on every append must not take readings from it.
    """
    return asyncio.get_running_loop().time()


def stream_due_for_renewal(started_at: float, now: float) -> bool:
    """True when a stream opened at *started_at* should be renewed at *now*.

    ``started_at`` 0 means no live stream was opened (no stream, or the
    ``chat.update`` fallback), which never needs renewal.
    """
    return started_at > 0 and now - started_at >= STREAM_RENEW_AFTER_S


def messages_noted() -> int:
    """The count of messages recorded so far, which a stream keeps when it opens."""
    return _noted


def note_thread_message(channel: str, thread_ts: str) -> None:
    """Record that a message the route took in has just landed in this thread."""
    global _noted
    _noted += 1
    key = (channel, thread_ts)
    _thread_message_no[key] = _noted
    _thread_message_no.move_to_end(key)
    while len(_thread_message_no) > MAX_NOTED_THREADS:
        _thread_message_no.popitem(last=False)


def message_landed_since(channel: str | None, thread_ts: str | None, opened_at: int | None) -> bool:
    """True when a recorded message landed in the thread after its stream opened.

    ``opened_at`` is the ``messages_noted()`` count the stream kept; None means
    no live stream was opened, and a stream outside a thread has no thread to read.
    """
    if not channel or not thread_ts or opened_at is None:
        return False
    return _thread_message_no.get((channel, thread_ts), 0) > opened_at

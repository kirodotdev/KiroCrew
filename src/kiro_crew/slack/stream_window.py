"""How long one Slack streaming message may stay open before it is renewed.

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
"""

from __future__ import annotations

import asyncio

#: Slack's hard limit on one stream's life, in seconds.
SLACK_STREAM_LIFETIME_S = 600.0

#: Renew a stream at its first append after this age, in seconds.
STREAM_RENEW_AFTER_S = 540.0

#: Appended to a running step's title on the message being closed, so the step
#: does not read as stuck there; the step goes on in the next message.
CONTINUES_BELOW = " · continues below"


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

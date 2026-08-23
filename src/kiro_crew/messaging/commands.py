"""Channel-neutral halves of the chat commands every DM channel ships.

A channel dispatcher's command handler is two things welded together: a decision
(what the grant becomes, whether a turn was actually cancelled, how long a login
link lives) and a send (which needs a ``chat_id``, a ``channel_id``, or a
``(conversation_id, serviceUrl)`` pair). The decision half is identical across
channels and this module owns it; only the send stays behind.

What lives here and what does NOT:

* HERE -- ``/stop``'s cooperative-cancel contract and lock ordering, ``/yolo``'s
  on/off/renew ladder with its SEL row, and the dashboard-link TTL vocabulary.
  Each returns the reply TEXT rather than sending it, the same shape
  :func:`~kiro_crew.messaging.link.release_conversation_location` already uses
  for ``/unlink``, so the user-facing strings have exactly one owner.
* NOT here -- anything address-shaped. Nothing below accepts a chat id, a thread
  or a service URL; ``/stop`` reaches its receipt bubble through the
  already-bound :class:`~kiro_crew.messaging.queue_receipt.ReceiptSurface`, which
  is what keeps Telegram's forum routing and Teams' service URLs channel-local.
* NOT here -- the command GRAMMAR. Which token a channel spells a command with,
  and how many words precede an argument, stays in that channel's own
  ``commands.py``; the functions below take an already-extracted argument.

Dependency direction is ``<channel> -> messaging`` (never the reverse), and this
module additionally imports nothing from ``kiro_crew.dashboard`` -- see
:func:`parse_dashboard_ttl` for the one place that constrains a signature.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.messaging.queue_receipt import ReceiptQueue, ReceiptSurface
from kiro_crew.safety_override import describe_grant_lifetime, safety_override
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


# ── /stop (hard cancel) ──────────────────────────────────────────────────────

#: Sent when the cooperative cancel reached a live turn.
STOP_REPLY_CANCELLED = "🛑 Stopped."

#: Sent when there was no live turn -- the queue is still cleared, and saying so
#: is what distinguishes "nothing to stop" from "the stop did not work".
STOP_REPLY_IDLE = "🛑 Nothing was running — queue cleared."


async def stop_running_turn(
    sessions: Any,
    session_key: str,
    *,
    queue: ReceiptQueue,
    surface: ReceiptSurface,
) -> str:
    """Abort the in-flight turn, drop the queue, finalize the receipt.

    Returns the reply text the channel should send; the send itself is the only
    address-shaped part and stays with the caller.

    **The cancel is cooperative before it is fatal.** ``cancel(wait_ack_timeout=0)``
    writes an ACP ``session/cancel`` notification and returns without waiting, so
    the acknowledgement to the user is immediate and the turn stops at its next
    safe point. On a shared runtime that is the only path that cannot take a
    co-tenant process down with it. ``cancel`` is probed with ``getattr`` because
    a provider that predates the ext-method simply does not offer one, and a
    missing cancel must degrade to "queue cleared" rather than raise at the user.

    ``clear_queue`` and the receipt finalize run under a SINGLE hold of
    :attr:`ReceiptQueue.lock`, which is the same hold the end-of-turn drain takes
    across its dequeue plus flip. Splitting them would let a drain observe a
    queue already emptied while its receipt bubble still said "⏳ Queued",
    orphaning the bubble as the durable record of a burst that was cancelled.

    A cancel that raises is logged and reported as "nothing was running": the
    queue is still cleared, so claiming a stop that did not happen would be the
    worse lie.
    """
    cancelled_turn = False
    if sessions.is_busy(session_key):
        provider = sessions.get_provider(session_key)
        cancel = getattr(provider, "cancel", None)
        if cancel is not None:
            try:
                await cancel(wait_ack_timeout=0)
                cancelled_turn = True
            except Exception:
                logger.warning(
                    "%s: stop could not cancel the running turn for %s",
                    surface.label,
                    session_key,
                    exc_info=True,
                )
    async with queue.lock:
        sessions.clear_queue(session_key)
        await queue.finish_cancelled_locked(session_key, surface)
    return STOP_REPLY_CANCELLED if cancelled_turn else STOP_REPLY_IDLE


# ── /yolo (the process-wide auto-approve grant) ──────────────────────────────

YOLO_ON = "on"
YOLO_OFF = "off"
YOLO_RENEW = "renew"

#: The verbs that MUTATE the grant. Anything else -- including a bare command and
#: a typo -- reports status instead, so an unrecognised word can never arm it.
YOLO_ACTIONS: frozenset[str] = frozenset((YOLO_ON, YOLO_OFF, YOLO_RENEW))


@dataclass(frozen=True)
class YoloPhrasing:
    """How a channel spells the ``/yolo`` command INSIDE its own reply text.

    The two spellings below are the whole per-channel difference in these
    replies, so they are data rather than a per-channel copy of the sentence: a
    channel picks a constant instead of restating the prose, which is how the
    three copies drifted in the first place.
    """

    #: How "turn it on" is written when the reply points the user at it.
    on_command: str
    #: The verb list on the usage line.
    usage: str


#: For a channel that renders no inline code (Telegram sends plain text here).
YOLO_PHRASING_PLAIN = YoloPhrasing(on_command="/yolo on", usage="/yolo on | off | renew")

#: For a channel whose messages render markdown inline code (Teams, Discord).
YOLO_PHRASING_MARKDOWN = YoloPhrasing(on_command="`/yolo on`", usage="`/yolo on` | `off` | `renew`")

#: What arming the grant actually covers, in one sentence. The grant is process-wide, so
#: a control that offers it has to say so BEFORE the press -- "auto-approve" on a button
#: inside one chat reads as scoped to that chat. Shared, so a channel's pre-press
#: affordance and the reply that confirms the grant cannot describe different scopes.
YOLO_SCOPE_NOTE = "Applies to every tool on every surface, this chat and the dashboard alike."


def parse_yolo_action(arg: str) -> str:
    """The first word of a ``/yolo`` argument, lowercased (``""`` when absent)."""
    words = arg.strip().lower().split()
    return words[0] if words else ""


async def run_yolo_command(
    arg: str,
    *,
    source: str,
    caller: str,
    phrasing: YoloPhrasing,
) -> str:
    """Report or change the process-wide auto-approve grant; return the reply.

    Reads and writes the SAME :func:`safety_override` grant the dashboard toggle
    and Slack's ``/kirocrew yolo`` drive, so a grant taken in one channel shows up
    -- and expires -- everywhere. *source* names the surface for both the grant's
    own audit and the SEL row; *caller* is the authorized identity that asked.

    Turning it on does NOT weaken the PreToolUse security gate: the
    sensitive-path keystone, the governance ceiling and the deny-list all run
    ahead of the auto-approve ladder in ``TurnDriver``, so a hard DENY still wins.

    The three mutators run off-loop: ``activate`` resolves the ad-hoc duration
    through a live config read and every one of them writes a SEL record
    (activation's is critical), so calling them inline would put filesystem
    latency on the gateway's single event loop and stall every other
    conversation and heartbeat task on a slow disk.

    The SEL row is written HERE so its shape cannot drift per channel. A caller
    that wants the outcome should read the reply rather than re-audit.
    """
    so = safety_override()
    action = parse_yolo_action(arg)

    if action not in YOLO_ACTIONS:
        status = f"ON 🟢 ({describe_grant_lifetime()})" if so.is_active() else "OFF 🔴"
        return f"YOLO is {status}.\nUsage: {phrasing.usage}"

    outcome = "allowed"
    if action == YOLO_ON:
        if so.is_active():
            reply = f"🟢 YOLO is already ON ({describe_grant_lifetime()})."
        elif (await asyncio.to_thread(so.activate, source)).active:
            reply = (
                f"🟢 YOLO ON ({describe_grant_lifetime()}) — every tool auto-approves. "
                f"{YOLO_SCOPE_NOTE} Denied-by-policy tools are still blocked."
            )
        else:
            reply = "❌ Couldn't turn YOLO on (audit system unavailable)."
            outcome = "denied"
    elif action == YOLO_OFF:
        # Unconditional: deactivate() also zeroes the deadline of a grant that
        # already lapsed, which closes the renew grace window so a later
        # "/yolo renew" cannot resurrect it, and records the operator's decision
        # either way.
        await asyncio.to_thread(so.deactivate, source)
        reply = "🔴 YOLO OFF — tools ask for approval again."
    else:
        renewed = (await asyncio.to_thread(so.renew, source)).renewed
        reply = (
            f"🟢 YOLO renewed ({describe_grant_lifetime()})."
            if renewed
            else f"🔴 YOLO is not active — use {phrasing.on_command} first."
        )
    sel().log_api_access(
        caller=caller,
        operation=f"{source}.yolo_mode",
        outcome=outcome,
        source=source,
        resources=f"yolo_{action}",
    )
    return reply


# ── dashboard login links ────────────────────────────────────────────────────

#: How long a presigned dashboard link lives when the user names no duration.
DEFAULT_DASHBOARD_TTL_SECS = 3600

#: Floor on a requested lifetime, in seconds. ``parse_duration`` accepts ``0h`` /
#: ``0m`` and answers 0 -- a real int, not ``None`` -- so without a floor it passes
#: every "did it parse" check and mints a bearer credential that is ALREADY
#: EXPIRED: a link the user cannot use, with no explanation of why. One minute is
#: the floor because it is the shortest lifetime the ``<N>h`` / ``<N>m`` grammar
#: can express, so clamping rejects exactly one input -- an explicit zero -- and
#: leaves every duration a user can type untouched.
#:
#: Clamping rather than falling back to the default is what keeps the reply
#: honest: the caller renders the GRANTED value with :func:`format_ttl`, which can
#: then never print ``0m``. Enforced HERE, in the shared parser, so it holds for
#: every channel that mints a link rather than for whichever one last had it
#: audited.
MIN_DASHBOARD_TTL_SECS = 60

#: ``"<N>h"`` / ``"<N>m"`` -> seconds, or None. Injected, see below.
DurationParser = Callable[[str], "int | None"]


def parse_dashboard_ttl(arg: str, *, parse_duration: DurationParser) -> int:
    """Resolve a dashboard-link TTL from a command's ARGUMENT string.

    *arg* is everything after the channel's own command tokens -- ``"2h"``,
    ``"30m"``, ``""`` when the user named no duration. Taking the argument rather
    than the whole message is what makes this channel-neutral: Telegram's grammar
    is ``/kirocrew dashboard <ttl>`` (the TTL is the third word) and Teams' is
    ``/dashboard <ttl>`` (the second), so a shared parser that indexed into the
    split message would be reading one channel's word count on the other's
    behalf. Only the FIRST word of *arg* is considered; anything after it is
    ignored rather than making the whole argument unparseable.

    *parse_duration* supplies the duration vocabulary and is injected rather than
    imported: it lives in ``dashboard/token_auth.py``, and ``kiro_crew.messaging``
    imports nothing from ``kiro_crew.dashboard`` -- that one-way direction is what
    keeps the transport layer independent of the surfaces built on top of it.
    Callers already import that module for ``generate_token``.

    Returns :data:`DEFAULT_DASHBOARD_TTL_SECS` when *arg* names no duration or the
    duration does not parse: a mistyped TTL should still hand the user a working
    link rather than refuse the command. A duration that parses is clamped up to
    :data:`MIN_DASHBOARD_TTL_SECS` -- see there for why an explicit zero must not
    reach the token minter.
    """
    words = arg.strip().split()
    if words:
        parsed = parse_duration(words[0].lower())
        if parsed is not None:
            return max(parsed, MIN_DASHBOARD_TTL_SECS)
    return DEFAULT_DASHBOARD_TTL_SECS


def format_ttl(ttl_secs: int) -> str:
    """Render a TTL in seconds as a human duration ("2h", 90m -> "1h 30m").

    Never truncates: a non-hour-multiple >= 1h renders both components so the
    reply reports exactly how long the login link stays live.
    """
    hours, rem = divmod(ttl_secs, 3600)
    mins = rem // 60
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"

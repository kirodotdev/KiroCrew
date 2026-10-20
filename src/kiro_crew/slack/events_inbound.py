"""The inbound-event checks the Socket Mode listener and the router share.

``SeenCache`` is the bounded event-id dedup set the gateway hands to both;
``TrustedBotTurnLedger`` and the process-wide ``_trusted_bot_turns`` are the loop guard
for mutually trusted gateways; ``_thread_follow_admits`` (with
``_addressed_to_someone_else``) decides whether thread-follow admits an unmentioned
message; ``_dm_single_session_enabled`` says whether a 1:1 DM runs as one flat session;
and ``_handle_message_deleted`` cancels a deleted message's queued or in-flight turn. The
listener's ``_background_tasks`` set and ``_maybe_prompt_owner`` live here too, and
importing this module sets the Slack SDK's WebSocket logger to WARNING, which drops its
routine reconnect noise.

Split out of ``kiro_crew.slack.events``, which stays the import path and the patch
target: every name here is reached as ``kiro_crew.slack.events.<name>``, and a patch
through that facade reaches this module (``kiro_crew._facade_mirror``). Nothing here
imports the facade. New inbound-check work goes here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from typing import TYPE_CHECKING

from kiro_crew.config.loader import ACTIVATION_REVIEW
from kiro_crew.sel import sel
from kiro_crew.session import unlink_queued_temp_paths
from kiro_crew.slack.enterprise import validated_self_user_id
from kiro_crew.slack.handler import is_allowed_user
from kiro_crew.slack.transport_dispatch import flat_dm_session_key

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger("kiro_crew.slack.events")


# Suppress noisy Slack SDK WebSocket reconnect errors — these are normal
# idle connection drops that the SDK handles automatically.
# WARNING lets ERROR through (recv failures, reconnect failures) while
# suppressing INFO (session established) and DEBUG (every message/ping).
logging.getLogger("slack_sdk.socket_mode.websockets").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Dedup cache — bounded LRU to avoid processing duplicate Slack events
# ---------------------------------------------------------------------------

_MAX_SEEN = 5000


# prevent GC of fire-and-forget tasks (Python event loop holds weak refs)
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


class SeenCache:
    """Bounded set that remembers the last *maxlen* event IDs."""

    def __init__(self, maxlen: int = _MAX_SEEN):
        self._d: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def check_and_add(self, key: str) -> bool:
        """Return ``True`` if *key* was already seen, else mark it."""
        if key in self._d:
            return True
        self._d[key] = None
        if len(self._d) > self._maxlen:
            self._d.popitem(last=False)
        return False

    def check(self, key: str) -> bool:
        """Return ``True`` if *key* was already marked, WITHOUT marking it.

        Split from ``check_and_add`` for the message-interceptor dedup, which must
        record a key ONLY on a non-PROCESS (challenge/deny) decision — marking a
        PROCESS message would wrongly drop the paired ``app_mention`` event (same
        ``msg_ts``) or a standalone inline message. See ``_route_message``.
        """
        return key in self._d

    def add(self, key: str) -> None:
        """Mark *key* seen (idempotent, bounded)."""
        if key not in self._d:
            self._d[key] = None
            if len(self._d) > self._maxlen:
                self._d.popitem(last=False)


class TrustedBotTurnLedger:
    """Bounded per-thread count of consecutive trusted-bot-initiated turns.

    The loop guard for mutually trusted gateways (slack.trusted_bot_ids):
    without a bound, two gateways that trust each other admit each other's
    replies as fresh turns indefinitely. Each admitted trusted-bot turn
    increments its thread's count; a processed message from an allowed human
    resets it; the admission gate in ``_route_message`` denies a trusted-bot
    message once the count reaches ``slack.trusted_bot_turn_limit``.

    LRU-bounded like ``SeenCache`` (single-event-loop access, no lock). An
    evicted thread restarts at zero — acceptable, because eviction requires
    *maxlen* other threads to have been active since, and the human reset
    remains available at any time.
    """

    def __init__(self, maxlen: int = _MAX_SEEN):
        self._d: OrderedDict[str, int] = OrderedDict()
        self._maxlen = maxlen

    def count(self, key: str) -> int:
        return self._d.get(key, 0)

    def increment(self, key: str) -> None:
        self._d[key] = self._d.pop(key, 0) + 1
        if len(self._d) > self._maxlen:
            self._d.popitem(last=False)

    def reset(self, key: str) -> None:
        self._d.pop(key, None)


#: Process-wide ledger: one gateway process serves one socket-mode connection,
#: and _route_message runs on its single event loop.
_trusted_bot_turns = TrustedBotTurnLedger()


# ---------------------------------------------------------------------------
# Tracking-channel join
# ---------------------------------------------------------------------------


def _maybe_prompt_owner(orch: GatewayOrchestrator, event: dict) -> None:
    """Multi-user access disabled — channel-join allowlist prompts are blocked."""
    return


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------


def _dm_single_session_enabled(orch: GatewayOrchestrator, channel: str) -> bool:
    """Whether a 1:1 DM in *channel* runs as one flat session.

    Two conditions, because only ``handle_message_transport`` honours the flat
    key. ``slack.dm_single_session`` alone is not enough: on the native path
    (``messaging.use_transport`` off, or a review-mode channel that
    ``_route_message`` deliberately keeps native) the turn runs under
    ``canonical_key(msg_ts)``, so bookkeeping keyed by channel would address a
    session that does not exist -- ``!stop`` pops the live task's entry, then
    finds no session and answers "Nothing running." while the turn keeps going.
    Deriving both conditions HERE keeps every call site in agreement instead of
    each one re-deciding.
    """
    if getattr(getattr(orch._cfg, "slack", None), "dm_single_session", False) is not True:
        return False
    if getattr(getattr(orch._cfg, "messaging", None), "use_transport", False) is not True:
        return False
    return orch._cfg.channel_config(channel).activation != ACTIVATION_REVIEW


async def _handle_message_deleted(orch: GatewayOrchestrator, event: dict) -> None:
    """Handle message_deleted subtype — cancel queued or in-flight messages."""
    deleted_ts = event.get("deleted_ts")
    _del_thread_ts = event.get("previous_message", {}).get("thread_ts")
    _del_channel = event.get("channel", "")
    _del_user = event.get("previous_message", {}).get("user", "")
    if deleted_ts and _del_channel and is_allowed_user(_del_user):
        # Same key the turn was queued under, or the cancellation misses it: a
        # deleted top-level message in a single-session DM belongs to the
        # channel's session, not to its own timestamp.
        _del_session_key = flat_dm_session_key(
            _del_channel, _del_thread_ts, enabled=_dm_single_session_enabled(orch, _del_channel)
        ) or (_del_thread_ts or deleted_ts)
        was_queued = False
        if orch.sessions:
            was_queued = orch.sessions.cancel_queued(_del_session_key, deleted_ts)
        if not was_queued:
            _pq = orch._pending_queue.get(_del_session_key, [])
            _filtered = [item for item in _pq if item[0] != deleted_ts]
            if len(_filtered) < len(_pq):
                was_queued = True
                # Dropped entries never reach _dispatch_queued's cleanup, so
                # their temp files must be unlinked here or they leak.
                for item in _pq:
                    if item[0] == deleted_ts:
                        unlink_queued_temp_paths(item[2])
                if _filtered:
                    orch._pending_queue[_del_session_key] = _filtered
                else:
                    orch._pending_queue.pop(_del_session_key, None)
        if was_queued:
            logger.info(
                "message_deleted: ts=%s session=%s queued=%s",
                deleted_ts,
                _del_session_key,
                was_queued,
            )
        sel().log_api_access(
            caller=event.get("previous_message", {}).get("user", "unknown"),
            operation="slack.message_deleted",
            outcome="allowed",
            source="slack",
            resources=f"ts={deleted_ts} session={_del_session_key} queued={was_queued}",
        )


#: A Slack user mention, ``<@U123>`` or ``<@U123|name>``; group 1 is the user id.
_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")

#: The run of user mentions a message opens with, the addressing position.
_LEADING_MENTIONS_RE = re.compile(r"\s*(?:<@[UW][A-Z0-9]+(?:\|[^>]*)?>\s*)+")


def _addressed_to_someone_else(text: str, self_uid: str) -> bool:
    """True when *text* opens with @-mentions and none of them is this bot.

    Only the leading run of mentions (after leading whitespace) addresses the
    message: ``<@U0OTHER> please verify`` is for U0OTHER, while ``please retry,
    cc <@U0OTHER>`` names someone in passing and is not. A message with no
    leading mention, or whose leading mentions include *self_uid*, is not
    addressed elsewhere. False when *self_uid* is unknown, so callers keep
    answering.
    """
    if not self_uid:
        return False
    leading = _LEADING_MENTIONS_RE.match(text or "")
    if leading is None:
        return False
    return self_uid not in _USER_MENTION_RE.findall(leading.group(0))


def _thread_follow_admits(
    orch: GatewayOrchestrator,
    *,
    thread_follow: bool,
    activation: str,
    thread_ts: str | None,
    text: str,
    sender_id: str,
    channel: str,
) -> bool:
    """Whether thread-follow admits an unmentioned message; logs the SEL denial when not.

    The one admission rule for every activation mode that answers followed-thread
    replies without an @-mention (mention, review, observe). The message must be
    a reply in a thread this bot already holds a session, session link, or
    conversation log for, with ``thread_follow`` on. A reply that opens with a
    mention of someone else is addressed to them and skipped, so answering does
    not talk over the addressee. A reply that opens with (or is) a mention of this
    bot is admitted by that same rule: Slack also delivers it as a plain
    ``message`` event, which reaches here with ``is_mention`` False.
    """
    in_active_thread = (
        thread_follow
        and thread_ts
        and orch.sessions
        and (
            orch.sessions.has_session(thread_ts)
            or orch.sessions.get_session_for_thread(thread_ts)
            or (orch.conv_log and orch.conv_log.has_log(thread_ts))
        )
    )
    if not in_active_thread:
        error = f"activation={activation}, no mention or active thread"
    elif _addressed_to_someone_else(text, validated_self_user_id()):
        error = "thread-follow: addressed to another user"
    else:
        return True
    sel().log_api_access(
        caller=sender_id,
        operation="slack.message",
        outcome="denied",
        source="slack",
        resources=channel,
        error=error,
    )
    return False

"""The pending-approval note the notification bridge carries (RFC bridge actions, B4p).

When a tool-approval card opens, this publishes one ``system.approval`` note naming
the request, so the bridge has something to carry to a chat app. It is behind the
default-off feature preview ``dashboard.feature_previews.bridge_approvals``: with the
preview off nothing is published.

The note is bridge-only. It goes straight to the notification bridge and never to
the dashboard's own sink, so the bell keeps ringing only when an approval expires
(the dashboard already shows a pending approval inline). Phase B4c binds the open
approval to a single-use token (:data:`ACTION_TOKEN_KEY`), so the Slack sink can
post Approve and Deny buttons that answer it.

A tool-approval note is told apart from the sealed-config review, the other
``system.approval`` producer, by its producer meta key (:data:`PRODUCER`), never by
its channel. When the card was already posted into a linked Slack thread, the note
says so (:data:`SLACK_CARD_KEY`), and the Slack sink reuses that card instead of
posting a second copy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: Hand-off tasks kept alive when the state holds no background-task set.
_HANDOFF_TASKS: "set[asyncio.Task[None]]" = set()

#: The feature preview that turns the note on (``dashboard.feature_previews``).
PREVIEW = "bridge_approvals"

#: The producer meta key, and the value that marks a pending tool approval.
PRODUCER_KEY = "producer"
PRODUCER = "tool_approval"

#: The meta key naming the request the note is about.
REQUEST_KEY = "approval_request_id"

#: The meta key set when the card is already posted in a linked Slack thread.
SLACK_CARD_KEY = "approval_slack_card"

#: The meta key carrying the action token a chat app's buttons answer with. It is
#: a field of its own, never part of the title or body a sink shows as text.
ACTION_TOKEN_KEY = "bridge_action_token"

#: The meta key carrying what the call will run (its input, else its title), set
#: only beside an action token: the Slack sink shows it above the buttons and a
#: reply transport's sink above its "reply approve or deny" line, so an Approve is
#: given on what it runs. No other sink renders it.
APPROVAL_DETAIL_KEY = "approval_detail"

#: What each bridged answer sets the prompt's decision future to: the values the
#: dashboard's own Approve and Deny buttons set.
DECISION_VALUES = {"approve": "approved", "deny": "rejected"}


def enabled(cfg: Any) -> bool:
    """Whether the preview is on in *cfg*. Off when unread."""
    from kiro_crew.edition_locks import feature_previews

    previews = feature_previews(getattr(getattr(cfg, "dashboard", None), "feature_previews", None))
    return isinstance(previews, Mapping) and previews.get(PREVIEW) is True


def is_pending_approval_note(note: Mapping[str, Any]) -> bool:
    """Whether *note* is this producer's pending tool-approval note."""
    return note.get("channel") == "system.approval" and note.get(PRODUCER_KEY) == PRODUCER


def publish_pending_approval(
    state: Any,
    cfg: Any,
    *,
    slot_key: str,
    session_key: str,
    request_id: str,
    tool_title: str,
    session_title: str = "",
    slack_card: bool = False,
    action_token: str = "",
    approval_detail: str = "",
) -> dict[str, Any] | None:
    """Hand one pending-approval note to the notification bridge. Never raises.

    *action_token* is the binding a chat app's Approve and Deny buttons answer
    (:func:`bind_pending_approval`); with none the note carries no buttons.
    *approval_detail* is what the call will run, carried only beside a token.

    Returns the note handed over, or None when the preview is off, the gateway has
    no bridge, or the note could not be built.
    """
    try:
        if not enabled(cfg) or getattr(state, "notification_bridge", None) is None:
            return None
        from kiro_crew.notifications.bus import NotificationBus, payload_from_legacy

        where = f" in {session_title}" if session_title else ""
        meta: dict[str, Any] = {
            PRODUCER_KEY: PRODUCER,
            REQUEST_KEY: str(request_id),
            "slot": slot_key,
            "session_key": session_key,
        }
        if slack_card:
            meta[SLACK_CARD_KEY] = True
        if action_token and approval_detail:
            meta[ACTION_TOKEN_KEY] = action_token
            meta[APPROVAL_DETAIL_KEY] = approval_detail
        payload = payload_from_legacy(
            "approval",
            "Approval needed",
            f"{tool_title or 'A tool'} is waiting for approval{where}.",
            meta,
        )
        # The bridge is the only sink: the dashboard bell never sees this note.
        return NotificationBus(sink=state._bridge_note).push(payload)
    except Exception:
        logger.warning("pending-approval bridge note failed", exc_info=True)
        return None


def bind_pending_approval(
    state: Any,
    decision: "asyncio.Future[Any]",
    *,
    session_key: str,
    request_id: str,
    deadline: float,
    crew_log_session: str = "",
    turn: int | None = None,
) -> str:
    """Bind *decision* so a chat app's Approve and Deny can answer it. Never raises.

    The binding is the gateway's ``BridgeActionRegistry``: a fresh random token,
    single-use, and dead at *deadline* (on ``bridge_actions.bridge_clock``), which
    is when the prompt stops waiting. Its recorded owner is Slack's configured
    ``owner_id``, else the owner DM of a reply transport
    (``bridge_transport.reply_owner``); each reader still checks its own owner.
    Returns the token, or "" when there is no registry or no owner anywhere, the
    deadline has passed, or the bind is refused: then the note carries no actions.
    """
    try:
        from kiro_crew.notifications.bridge_actions import bridge_clock
        from kiro_crew.notifications.bridge_transport import REPLY_TRANSPORTS, reply_owner

        registry = getattr(state, "bridge_actions", None)
        owner = getattr(state, "owner_id", "")
        if not isinstance(owner, str) or not owner:
            owner = next(
                (found for name in REPLY_TRANSPORTS if (found := reply_owner(state, name))), ""
            )
        remaining = deadline - bridge_clock()
        if registry is None or not owner or remaining <= 0:
            return ""
        token = registry.bind(
            session_key,
            request_id,
            decision,
            choices=DECISION_VALUES,
            ttl_s=remaining,
            crew_log_session=crew_log_session,
            turn=turn,
            owner=owner,
        )
        return token if isinstance(token, str) else ""
    except Exception:
        logger.warning("pending-approval bridge binding failed", exc_info=True)
        return ""


def schedule_pending_approval(
    state: Any,
    decision: "asyncio.Future[Any]",
    *,
    answerable: bool = False,
    ttl_s: float = 0.0,
    crew_log_session: str = "",
    turn: int | None = None,
    **fields: Any,
) -> "asyncio.Task[None] | None":
    """Start the hand-off of one open approval as its own task, and return it.

    The permission prompt calls this and never awaits the result: it registered its
    approval and must reach the ``try`` that cleans it up with no new await in
    between, or a cancel landing there would strand the approval. The task reads
    the live config off the loop, sends nothing for an approval answered in the
    meantime, and is held until done (the gateway's background set, else this
    module's) with any failure logged. *answerable* says a plain Approve or Deny
    settles this prompt; a deny-rule override card, which the dashboard alone
    answers, passes False and its note gets no buttons. *ttl_s* is how long the
    prompt waits, from now, and *crew_log_session* / *turn* name the turn the
    answer is audited under; together they bind the approval when actions are on
    (:func:`bind_pending_approval`). *fields* are :func:`publish_pending_approval`'s
    keywords.
    """
    from kiro_crew.notifications.bridge_actions import bridge_clock

    binding = (
        {
            "deadline": bridge_clock() + ttl_s,
            "crew_log_session": crew_log_session,
            "turn": turn,
        }
        if answerable
        else None
    )
    try:
        task = asyncio.get_running_loop().create_task(_hand_off(state, decision, fields, binding))
    except RuntimeError:
        return None  # No running loop: nothing can bridge.
    tasks = getattr(state, "_background_tasks", None)
    if not isinstance(tasks, set):
        tasks = _HANDOFF_TASKS
    # Held until done: the loop keeps only a weak reference to a task.
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_failure)
    return task


async def _hand_off(
    state: Any,
    decision: "asyncio.Future[Any]",
    fields: dict[str, Any],
    binding: dict[str, Any] | None,
) -> None:
    from kiro_crew.config import KiroCrewConfig

    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        return  # No config, no preview: nothing is bridged.
    if decision.done() or not enabled(cfg):
        return  # Answered while the config was read, or the preview is off.
    token = ""
    # Actions are offered only beside what they approve. A card already in a linked
    # Slack thread is still bound: the Slack sink posts nothing for it, and a reply
    # transport can still answer the same decision.
    detail = fields.get("approval_detail")
    has_detail = isinstance(detail, str) and bool(detail.strip())
    if binding is not None and has_detail:
        token = bind_pending_approval(
            state,
            decision,
            session_key=str(fields.get("session_key") or ""),
            request_id=str(fields.get("request_id") or ""),
            **binding,
        )
    publish_pending_approval(state, cfg, action_token=token, **fields)


def _log_failure(task: "asyncio.Task[None]") -> None:
    """Log a hand-off that raised, so a failure is never silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("pending-approval bridge hand-off failed", exc_info=exc)

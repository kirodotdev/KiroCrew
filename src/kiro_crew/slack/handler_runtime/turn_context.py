"""The Slack thread context a native turn's prompt carries.

The thread's first message when a new Slack-born session opens in a thread it did not
start (``slack/thread_parent.py``, which also records it as the transcript's first row),
the replies posted since this conversation's last turn in the thread
(``slack/thread_replies.py``), and the ``conversations.replies`` fallback line that
stands in for the parent when its fetch found nothing. ``handle_message`` passes each
result to ``ContextBuilder.build_message``.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        ContextBuilder,
        ConversationLog,
        SlackClientOps,
        ThreadReplies,
        _is_slack_restricted,
        fetch_thread_parent,
        has_noted_turn,
        has_prior_turns,
        is_slack_born,
        logger,
        parent_prompt_text,
        record_thread_parent,
        redact,
        replies_since_last_turn,
    )


async def _thread_context(
    slack: SlackClientOps,
    channel: str,
    thread_ts: str | None,
    msg_ts: str,
    session_key: str,
    *,
    is_new: bool,
    resumed: bool,
    compressed: str | None,
    context_builder: ContextBuilder | None,
    conversation_log: ConversationLog | None,
    agent: str | None,
) -> tuple[str | None, ThreadReplies | None]:
    """The thread parent and the unseen replies for this turn's prompt.

    Returns ``(thread_parent_text, thread_replies)``.
    """
    # Fetch thread parent message when starting a new session in an
    # existing thread (e.g. replying to a cron thread).  Gives the LLM
    # context about what started the thread without requiring a manual
    # thread-reading tool call. This path persists the user's row only
    # after the turn, so ``compressed`` is non-empty only when earlier
    # turns exist. A Slack-born session also records the parent as the
    # transcript's first row (see ``slack/thread_parent.py``).
    thread_parent_text: str | None = None
    # One transcript read serves the parent and the thread-replies checks.
    _prior: bool | None = None
    if is_new and not resumed and thread_ts and context_builder:
        if not compressed:
            _record_parent = bool(
                conversation_log
                and thread_ts != msg_ts
                and is_slack_born(session_key)
                and not _is_slack_restricted(session_key)
                and not (_prior := await has_prior_turns(conversation_log, session_key))
            )
            _thread_parent = await fetch_thread_parent(
                slack, channel, thread_ts, with_author=_record_parent
            )
            if _thread_parent is not None:
                thread_parent_text = parent_prompt_text(_thread_parent)
                if _record_parent:
                    assert conversation_log is not None
                    await record_thread_parent(
                        conversation_log, session_key, _thread_parent, agent=agent
                    )

    # Thread replies since this conversation's last turn in the thread
    # (``slack/thread_replies.py``). Context only: who gets answered was
    # decided before this point.
    _thread_replies: ThreadReplies | None = None
    if context_builder and thread_ts and thread_ts != msg_ts:
        if _prior is None and not has_noted_turn(session_key, thread_ts):
            _prior = await has_prior_turns(conversation_log, session_key)
        _first_turn = not has_noted_turn(session_key, thread_ts) and not _prior
        _thread_replies = await replies_since_last_turn(
            slack,
            channel,
            thread_ts,
            msg_ts,
            session_key=session_key,
            first_turn=_first_turn,
        )
    return thread_parent_text, _thread_replies


async def _thread_meta_fallback(
    slack: SlackClientOps,
    channel: str,
    thread_ts: str | None,
    *,
    is_new: bool,
    resumed: bool,
    thread_parent_text: str | None,
    compressed: str | None,
    context_builder: Any,
) -> str | None:
    """The ``[Thread has N replies. Parent message: ...]`` line, or None.

    Fallback thread metadata: when thread_parent_text is unavailable
    (e.g. fetch_message failed), try conversations.replies to get parent info.
    Note: requires channels:history (public) or groups:history (private). Both
    ship in the manifest, but installs created before groups:history was added
    need a reinstall to gain it. Gracefully degrades — if scope is missing,
    thread context is simply skipped.
    """
    _thread_meta: str | None = None
    if (
        is_new
        and not resumed
        and thread_ts
        and not thread_parent_text
        and not compressed
        and context_builder
    ):
        replies = await slack.fetch_thread_replies(
            channel, thread_ts, limit=1, warn_on_pagination=False
        )
        if replies:
            parent = replies[0]
            reply_count = parent.get("reply_count", 0)
            parent_text = redact(parent.get("text", ""))
            if parent_text:
                if len(parent_text) > 500:
                    parent_text = parent_text[:500] + "…[truncated]"
                if reply_count > 0:
                    _thread_meta = (
                        f'[Thread has {reply_count} replies. Parent message: "{parent_text}"]\n'
                        "Use the Slack MCP server's thread-reading tool (e.g. get_thread) "
                        "to read the full thread if needed.\n"
                    )
                else:
                    _thread_meta = f'[Parent message: "{parent_text}"]\n'
        else:
            logger.info(
                "Thread fallback returned no replies for %s/%s (missing scope?)",
                channel,
                thread_ts,
            )
    return _thread_meta

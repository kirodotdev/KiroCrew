"""Session handoff between Dashboard and Slack.

Two capabilities:
1. Dashboard → Slack: POST /api/chat/slots/{slot}/handoff creates a Slack
   DM thread and symlinks the JSONL history so the user can continue.
2. Slack !resume: lists recent sessions, symlinks the chosen one's JSONL
   to the current thread so context_builder injects the full history.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)


def list_recent_sessions(
    conversation_log: ConversationLog,
    limit: int = 10,
) -> list[dict]:
    """Return recent sessions across all surfaces, newest first."""
    sessions = conversation_log.list_sessions()
    result: list[dict] = []
    for s in sessions[: limit * 2]:
        if len(result) >= limit:
            break
        key = s.get("key", "")
        if not key:
            continue
        if key.startswith("dashboard:") or key.startswith("dashboard_"):
            source = "dashboard"
        elif key.startswith("cron:") or key.startswith("cron_"):
            source = "cron"
        else:
            source = "slack"
        result.append(
            {
                "key": key,
                "title": s.get("title", key),
                "source": source,
                "modified": s.get("modified", 0),
            }
        )
    return result


def format_session_list(sessions: list[dict]) -> str:
    """Format sessions as a numbered Slack message."""
    if not sessions:
        return "No recent sessions found."
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    lines = ["*Recent sessions:*\n"]
    now = time.time()
    for i, s in enumerate(sessions, 1):
        age_mins = int((now - s["modified"]) / 60)
        if age_mins < 60:
            age = f"{age_mins}m ago"
        elif age_mins < 1440:
            age = f"{age_mins // 60}h ago"
        else:
            age = f"{age_mins // 1440}d ago"
        icon = {"dashboard": "🖥️", "slack": "💬", "cron": "⏰"}.get(s["source"], "📝")
        title = s["title"][:60] or s["key"][:40]
        title, _ = redact_credentials(title)
        title, _ = redact_exfiltration_urls(title)
        lines.append(f"`{i}` {icon} {title}  _{age}_")
    lines.append("\n_Click a session's resume button to continue._")
    return "\n".join(lines)


async def handoff_to_slack(
    slack: SlackClientOps,
    owner_id: str,
    conversation_log: ConversationLog,
    session_key: str,
    title: str = "",
    channel: str | None = None,
    sessions: object | None = None,
    transcript_key: str = "",
) -> str | None:
    """Hand off a dashboard session to a new Slack DM thread.

    Links the session via ``set_slack_link`` so bidirectional sync works.
    Returns the thread_ts, or None on failure.

    *transcript_key* is where the conversation is STORED, when that differs from
    the session it runs on. They differ for an unbound channel tab: it runs under
    ``dashboard:<stem>`` but its transcript is the channel's own file, so reading
    the seed messages under *session_key* found nothing and handed off an empty
    thread. Defaults to *session_key*, which is correct for every other slot.
    """
    read_key = transcript_key or session_key
    messages = conversation_log.read_messages(read_key)
    if not messages:
        return None

    if not title:
        meta = conversation_log.get_metadata(read_key)
        title = meta.get("title", session_key)

    preview = ""
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            preview = m["content"][:120]
            break

    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    title, _ = redact_credentials(title)
    title, _ = redact_exfiltration_urls(title)
    preview, _ = redact_credentials(preview)
    preview, _ = redact_exfiltration_urls(preview)

    try:
        target_channel = channel or await slack.open_dm(owner_id)
        thread_ts = await slack.post_message(
            target_channel,
            f"📲 *{title}*\n>{preview}\n\n_Reply to continue this session._",
        )

        # Link via SessionMap instead of symlink
        if sessions and hasattr(sessions, "set_slack_link"):
            try:
                sessions.set_slack_link(session_key, thread_ts, target_channel)  # type: ignore[union-attr]
            except Exception:
                logger.warning("Slack thread created but session link failed", exc_info=True)

        return thread_ts
    except Exception:
        logger.warning("Handoff to Slack failed", exc_info=True)
        return None


# ── Resume state: track pending !resume selections ──
_PENDING_RESUME_TTL = 5 * 60  # 5 minutes

_pending_resumes: dict[str, tuple[float, list[dict]]] = {}
_pending_resume_msg_ts: dict[str, tuple[str, str]] = {}  # session_key → (bot_list_ts, user_cmd_ts)


def set_pending_resume(
    session_key: str,
    sessions: list[dict],
    bot_list_ts: str = "",
    user_cmd_ts: str = "",
) -> None:
    """Store the session list so a follow-up number reply can resolve it."""
    _pending_resumes[session_key] = (time.monotonic(), sessions)
    if bot_list_ts:
        _pending_resume_msg_ts[session_key] = (bot_list_ts, user_cmd_ts)


def peek_pending_resume(session_key: str) -> list[dict] | None:
    """Return pending resume sessions without consuming them, or None."""
    entry = _pending_resumes.get(session_key)
    if entry is None:
        return None
    created_at, sessions = entry
    if time.monotonic() - created_at > _PENDING_RESUME_TTL:
        _pending_resumes.pop(session_key, None)
        _pending_resume_msg_ts.pop(session_key, None)
        return None
    return sessions


def pop_pending_resume(session_key: str) -> list[dict] | None:
    """Pop and return pending resume sessions, or None if expired."""
    _pending_resume_msg_ts.pop(session_key, None)  # always clean
    entry = _pending_resumes.pop(session_key, None)
    if entry is None:
        return None
    created_at, sessions = entry
    if time.monotonic() - created_at > _PENDING_RESUME_TTL:
        return None
    return sessions


def pop_pending_resume_ts(session_key: str) -> tuple[str, str]:
    """Pop and return (bot_list_ts, user_cmd_ts) for cleanup."""
    return _pending_resume_msg_ts.pop(session_key, ("", ""))

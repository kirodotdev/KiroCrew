"""Wrap-up: a summary with an Archive button, then the archive a person asks for.

Slack's lifecycle ends a code channel this way: the agent posts a final summary,
the people in the channel confirm, and the app calls
``agents.conversations.archive`` with that summary's ts, so the summary stays the
channel's ``summary_message`` after it is archived.

**Posting the summary.** ``/wrap-up`` asks the bound session to finish up and
call the ``code_channel_wrap_up`` tool with what it did; the tool posts
:func:`wrap_up_blocks` -- the agent's summary, then the facts this gateway knows
first-hand (branch, PR, CI, uncommitted changes) -- with the Archive button, and
records it as the channel's ``summary_message``. If the turn ``/wrap-up`` started
ends without that tool call, :func:`on_turn_end` posts the facts alone, so the
button always arrives. Posting it marks the dashboard session wrapped up
(``Binding.wrapped_up_at``). A newer wrap-up makes the older one's button stale
(a click on it is refused); the older message itself is never edited.

**Archiving.** A click (:func:`handle_archive_click`) is authorized like a
message in the channel -- an allowed user, and an ORIGIN conversation that still
admits the bot -- and only the button on the CURRENT wrap-up counts. Under a
per-channel lock it replaces the button (a second click finds nothing to press),
archives with ``summary_message_ts``, then hands the worktree to the workspace's
cleanup, which removes it only when nothing would be lost: a tree with
uncommitted work is kept on disk and the dashboard is told where. Every decision
is SEL-audited.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.models import CodeChannelApiError
from kiro_crew.slack.code_channels.store import Binding, store
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

ARCHIVE_ACTION_ID = "kc_code_channel_archive"
#: Slack caps a section's text at 3,000 characters; the heading takes a few.
MAX_SUMMARY_CHARS = 2800

DEFAULT_SUMMARY = "Here is where this session stands."
ARCHIVE_CONFIRM_TEXT = (
    "Everyone in this channel loses the composer; its history, views and context bar "
    "stay readable. The worktree is removed only when it holds no uncommitted work."
)

_locks: dict[str, asyncio.Lock] = {}
#: Turn starts, numbered in order (one counter for the process).
_seq = [0]
_turn_started: dict[str, int] = {}
#: The last turn number a channel had started when its /wrap-up arrived.
_requested_after: dict[str, int] = {}


def _lock(channel_id: str) -> asyncio.Lock:
    lock = _locks.get(channel_id)
    if lock is None:
        lock = _locks[channel_id] = asyncio.Lock()
    return lock


# ── Facts ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Facts:
    """What the gateway knows first-hand about the channel's work."""

    branch: str = ""
    pr_number: int = 0
    pr_url: str = ""
    pr_state: str = ""
    pr_ci: str = ""
    changes: str = ""
    #: None when git could not say.
    uncommitted: bool | None = None
    worktree: str = ""
    #: One outcome line per worker of a conductor-led channel
    #: (``• Fix login: PR #12 merged``), already escaped and redacted.
    workers: tuple[str, ...] = ()


def _gather_facts_sync(binding: Binding) -> Facts:
    from kiro_crew.slack.code_channels.workspace import git_ops
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    record = WorkspaceStore().get(binding.team_id, binding.channel_id)
    if record is None:
        return Facts()
    facts = Facts(
        branch=record.branch,
        pr_number=record.pr_number,
        pr_url=record.pr_url,
        pr_state=record.pr_state,
        pr_ci=record.pr_ci,
        worktree=record.worktree,
    )
    tree = record.worktree
    if not tree:
        return facts
    try:
        branch = git_ops.current_branch(tree) or record.branch
        probe = git_ops.run_git(
            ["--no-optional-locks", "status", "--porcelain", "--ignore-submodules=all"], tree
        )
        status = probe.stdout if probe.returncode == 0 else None
        changes = ""
        if record.base_sha:
            proc = git_ops.run_git(
                ["--no-optional-locks", "diff", "--shortstat", record.base_sha], tree
            )
            if proc.returncode == 0:
                changes = proc.stdout.strip()
    except Exception:
        logger.debug(
            "code channel %s: wrap-up facts unavailable", binding.channel_id, exc_info=True
        )
        return facts
    return replace(
        facts,
        branch=branch,
        changes=changes,
        uncommitted=None if status is None else bool(status.strip()),
    )


async def gather_facts(binding: Binding) -> Facts:
    try:
        facts = await asyncio.to_thread(_gather_facts_sync, binding)
    except Exception:
        facts = Facts()
    try:
        from kiro_crew.slack.code_channels import board

        lines = await board.outcome_lines(binding)
    except Exception:
        logger.debug("code channel %s: worker outcomes unavailable", binding.channel_id)
        lines = []
    return replace(facts, workers=tuple(lines)) if lines else facts


_PR_WORDS = {"open": "open", "draft": "draft", "merged": "merged", "closed": "closed"}
_CI_WORDS = {"passed": "tests passing", "failed": "tests failing", "running": "tests running"}


def _fact_lines(facts: Facts) -> list[str]:
    parts: list[str] = []
    if facts.branch:
        parts.append(f"Branch `{facts.branch}`")
    if facts.pr_number and facts.pr_url.startswith("https://"):
        state = _PR_WORDS.get(facts.pr_state, "")
        pr = f"<{facts.pr_url}|PR #{facts.pr_number}>"
        parts.append(f"{pr} {state}".strip())
        ci = _CI_WORDS.get(facts.pr_ci, "")
        if ci and facts.pr_state in ("open", "draft"):
            parts.append(ci)
    elif facts.branch:
        parts.append("no pull request yet")
    lines = [" · ".join(parts)] if parts else []
    if facts.changes:
        lines.append(f"Changed since the branch was cut: {facts.changes}")
    if facts.uncommitted:
        lines.append("⚠️ The worktree has uncommitted changes. Archiving keeps them on disk.")
    if facts.workers:
        lines.append("*Workers*\n" + _bounded_lines(facts.workers, _MAX_WORKER_CHARS))
    return lines


#: Room the worker outcomes may take in the facts block (Slack caps a text at 3,000).
_MAX_WORKER_CHARS = 1800


def _bounded_lines(lines: tuple[str, ...], limit: int) -> str:
    """*lines* joined, whole lines only, within *limit* characters, then ``+N more``."""
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    rest = len(lines) - len(kept)
    return "\n".join(kept + ([f"+{rest} more"] if rest else []))


_BROADCAST = re.compile(r"<!(channel|here|everyone)(?:\|[^>]*)?>")
_GROUP_PING = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")


def _redact(text: str) -> str:
    """Agent-authored text for Slack: exfiltration URLs and credentials redacted
    (every redaction pass), and no channel-wide or user-group pings."""
    from kiro_crew.security import redact

    text = _BROADCAST.sub(r"@\1", redact(text))
    return _GROUP_PING.sub(lambda m: m.group(1) or "@group", text)


def wrap_up_blocks(summary: str, facts: Facts, channel_id: str) -> list[dict[str, Any]]:
    """The wrap-up message: summary, facts, and the Archive button."""
    body = _redact(summary.strip() or DEFAULT_SUMMARY)[:MAX_SUMMARY_CHARS]
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Wrap-up*\n{body}"}},
    ]
    # Branch names and git output are agent-influenced too: same passes as the summary.
    lines = [_redact(line) for line in _fact_lines(facts)]
    if lines:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(lines)}]}
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ARCHIVE_ACTION_ID,
                    "value": channel_id,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Archive channel"},
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Archive this channel?"},
                        "text": {"type": "plain_text", "text": ARCHIVE_CONFIRM_TEXT},
                        "confirm": {"type": "plain_text", "text": "Archive"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                }
            ],
        }
    )
    return blocks


def _retired_blocks(blocks: list[dict[str, Any]], note: str) -> list[dict[str, Any]]:
    """*blocks* with the Archive button replaced by *note*."""
    kept = [b for b in blocks if b.get("type") != "actions"]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return kept


# ── Posting ───────────────────────────────────────────────────────────────


async def request_wrap_up(channel_id: str, user_id: str) -> None:
    """``/wrap-up`` asked the agent for a summary; the turn end checks it arrived.

    Ordered against turn starts by a counter, not the clock, so a turn that began
    before the request can never be read as the request's own turn.
    """
    seq = _last_turn_seq()  # read before awaiting: a turn may start meanwhile
    await update_async(channel_id, wrapup_pending_since=time.time(), wrapup_requested_by=user_id)
    _requested_after[channel_id] = seq  # published only once the request is stored


async def cancel_wrap_up(channel_id: str) -> None:
    """The ``/wrap-up`` prompt never reached the session: nothing to fall back for."""
    await update_async(channel_id, wrapup_pending_since=0.0, wrapup_requested_by="")
    _requested_after.pop(channel_id, None)


def _mark_archived(channel_id: str, stamp: float) -> None:
    """Stamp the binding archived in one locked step (never rewrites other fields)."""
    store().update_binding(channel_id, archived_at=stamp)


async def _stamp_archived(channel_id: str, stamp: float, attempts: int = 2) -> bool:
    """Write ``Binding.archived_at``; False when every attempt failed."""
    for _ in range(attempts):
        try:
            await asyncio.to_thread(_mark_archived, channel_id, stamp)
            return True
        except Exception:
            logger.warning("code channel %s: archive stamp not written", channel_id, exc_info=True)
    return False


def _mark_wrapped(channel_id: str, stamp: float) -> None:
    """Stamp the binding wrapped up in one locked step (never rewrites other fields)."""
    store().update_binding(channel_id, wrapped_up_at=stamp)


async def post_wrap_up(binding: Binding, summary: str, *, caller: str) -> str:
    """Post the wrap-up in *binding*'s channel; returns its ts ("" when it failed)."""
    slack = surface.slack()
    if slack is None:
        return ""
    channel = binding.channel_id
    if not await surface.outbound_permitted():
        surface.audit(
            caller,
            "slack.code_channel.wrap_up",
            "denied",
            f"channel={channel}",
            "channels governance policy",
        )
        return ""
    facts = await gather_facts(binding)
    blocks = wrap_up_blocks(summary, facts, channel)
    fallback = "Wrap-up: " + _redact(summary.strip() or DEFAULT_SUMMARY)[:200]
    async with _lock(channel):
        if await surface.is_archived(binding):
            surface.audit(
                caller, "slack.code_channel.wrap_up", "denied", f"channel={channel}", "archived"
            )
            return ""
        try:
            ts = str(await slack.post_blocks(channel, blocks, fallback) or "")
        except Exception:
            logger.warning("code channel %s: wrap-up post failed", channel, exc_info=True)
            ts = ""
        if not ts:
            surface.audit(
                caller, "slack.code_channel.wrap_up", "error", f"channel={channel}", "post failed"
            )
            return ""
        now = time.time()
        await update_async(
            channel,
            summary_ts=ts,
            summary_session_key=binding.session_key,
            wrapup_pending_since=0.0,
            wrapup_requested_by="",
        )
        _requested_after.pop(channel, None)
        await asyncio.to_thread(_mark_wrapped, channel, now)
        surface.push_slots(surface.dashboard_state())
    client = surface.client()
    if client is not None:
        try:
            await client.set_summary_message(channel, ts)
        except CodeChannelApiError as exc:
            logger.info("code channel %s: summary_message not recorded: %s", channel, exc.code)
        else:
            try:
                await update_async(channel, summary_message_ts=ts)
            except Exception:
                logger.debug("code channel %s: summary pointer not stored", channel)
    surface.audit(caller, "slack.code_channel.wrap_up", "allowed", f"channel={channel} ts={ts}")
    return ts


def _last_turn_seq() -> int:
    return _seq[0]


async def on_turn_start(binding: Binding, _event: Any) -> None:
    _seq[0] += 1
    _turn_started[binding.channel_id] = _seq[0]


async def on_turn_end(binding: Binding, event: Any) -> None:
    """The turn ``/wrap-up`` started ended without the summary: post the facts."""
    channel = binding.channel_id
    started = _turn_started.pop(channel, 0)
    record = await get_async(channel)
    if record is None or not record.wrapup_pending_since:
        return
    if await surface.is_archived(binding):
        return
    after = _requested_after.get(channel)
    if after is not None and started <= after:
        return  # an earlier turn ended; the /wrap-up turn has not run yet
    if getattr(event, "outcome", "ok") != "ok" or surface.channel_stopped(channel):
        await cancel_wrap_up(channel)
        return
    await post_wrap_up(binding, DEFAULT_SUMMARY, caller=record.wrapup_requested_by or "slack")


# ── Archive ───────────────────────────────────────────────────────────────

ALREADY_ARCHIVED_TEXT = "This channel is already archived."
STALE_TEXT = "That wrap-up is out of date. Run `/wrap-up` for a fresh one."
NOT_ALLOWED_TEXT = "⛔ You can't archive this channel."
ARCHIVE_FAILED_TEXT = "I couldn't archive this channel ({code}). Try again in a moment."


async def _ephemeral(channel: str, user: str, text: str) -> None:
    slack = surface.slack()
    if slack is None or not (channel and user):
        return
    try:
        await slack.post_ephemeral(channel, user, text)
    except Exception:
        logger.debug("code channel ephemeral failed", exc_info=True)


def _payload_ids(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    channel = str((payload.get("channel") or {}).get("id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    msg_ts = str((payload.get("message") or {}).get("ts") or "")
    actions = payload.get("actions") or [{}]
    value = str((actions[0] or {}).get("value") or "")
    return channel, user, msg_ts, value


def _notify_unrecorded(binding: Binding) -> None:
    state = surface.dashboard_state()
    notify = getattr(state, "notify", None)
    if callable(notify):
        try:
            notify(
                "slack",
                "Code channel archived; not recorded",
                "Slack archived the channel but Kiro Crew could not record it, so "
                "nothing was cleaned up and its worktree is still in place. Slack's "
                "archive event records it when it arrives.",
                meta={"channel_id": binding.channel_id, "session_key": binding.session_key},
            )
        except Exception:
            logger.debug("unrecorded-archive notification failed", exc_info=True)


def _notify_kept(binding: Binding, path: str, reason: str) -> None:
    state = surface.dashboard_state()
    notify = getattr(state, "notify", None)
    if not callable(notify):
        return
    try:
        notify(
            "slack",
            "Code channel archived; worktree kept",
            f"The worktree at {path} was left in place: {reason}.",
            meta={"channel_id": binding.channel_id, "session_key": binding.session_key},
        )
    except Exception:
        logger.debug("worktree-kept notification failed", exc_info=True)


async def handle_archive_click(orch: Any, payload: dict[str, Any]) -> bool:
    """An Archive click; False when it is not ours to answer (the gate is shut)."""
    if not code_channels_on(live_config(orch)):
        return False
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit
    from kiro_crew.slack.handler_runtime.access import is_allowed_user

    channel, user, msg_ts, value = _payload_ids(payload)
    resources = f"channel={channel} message={msg_ts}"
    operation = "slack.code_channel.archive"
    team = str(
        (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    )
    if not surface.workspace_admitted(team):
        surface.audit(
            user, operation, "denied", f"{resources} team_id={team}", "enterprise_origin_mismatch"
        )
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or value != channel:
        surface.audit(user, operation, "denied", resources, "not a bound code channel")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    if not is_allowed_user(user):
        surface.audit(user, operation, "denied", resources, "unauthorized user")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    origin = _origin_of(channel, binding, None)
    # The code channel's own activation applies on top of its origin's, exactly
    # as on the message path (routing.inbound_gate).
    if not channel_and_origin_permit(live_config(orch), channel, origin):
        surface.audit(
            user,
            operation,
            "denied",
            f"{resources} origin={origin}",
            "channel or origin does not admit the bot",
        )
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    if not await surface.outbound_permitted():
        surface.audit(user, operation, "denied", resources, "channels governance policy")
        return True
    async with _lock(channel):
        binding = await asyncio.to_thread(store().get_binding, channel) or binding
        record = await get_async(channel)
        if await surface.is_archived(binding):
            surface.audit(user, operation, "allowed", f"{resources} result=already_archived")
            await _ephemeral(channel, user, ALREADY_ARCHIVED_TEXT)
            return True
        if (
            record is None
            or not record.summary_ts
            or record.summary_ts != msg_ts
            or record.summary_session_key != binding.session_key
        ):
            surface.audit(user, operation, "denied", resources, "not the current wrap-up")
            await _ephemeral(channel, user, STALE_TEXT)
            return True
        client, slack = surface.client(), surface.slack()
        if client is None or slack is None:
            surface.audit(user, operation, "error", resources, "no Slack client")
            await _ephemeral(channel, user, ARCHIVE_FAILED_TEXT.format(code="not connected"))
            return True
        original = list((payload.get("message") or {}).get("blocks") or [])
        archived_note = f"Archived by <@{user}>."
        try:
            # Before the archive: an archived channel takes no edits, and the
            # button must be gone before a second click can land.
            await slack.update_message(
                channel,
                msg_ts,
                "Wrap-up (archived)",
                blocks=_retired_blocks(original, archived_note),
            )
        except Exception:
            logger.debug("wrap-up button retire failed", exc_info=True)
        try:
            await client.archive(channel, summary_message_ts=msg_ts)
        except CodeChannelApiError as exc:
            if exc.code != "already_archived":
                surface.audit(user, operation, "error", resources, exc.code)
                if original:
                    try:
                        await slack.update_message(channel, msg_ts, "Wrap-up", blocks=original)
                    except Exception:
                        logger.debug("wrap-up button restore failed", exc_info=True)
                await _ephemeral(channel, user, ARCHIVE_FAILED_TEXT.format(code=exc.code))
                return True
        # Slack has archived the channel: record it first, so nothing below can
        # leave the archive unrecorded or unaudited.
        now = time.time()
        # Fenced in this process at once, so no tool, command or wrap-up acts on
        # the channel even while the stamp below is being written.
        surface.fence_archived(channel)
        if not await _stamp_archived(channel, now):
            # Archived in Slack but not recorded: nothing is cleaned up on an
            # unrecorded archive; a later click (already_archived) retries.
            surface.audit(
                user, operation, "error", f"{resources} result=archived", "archive not recorded"
            )
            _notify_unrecorded(binding)
            return True
        surface.unfence(channel)
        surface.audit(user, operation, "allowed", f"{resources} result=archived")
        try:
            await cancel_wrap_up(channel)
        except Exception:
            logger.warning("code channel %s: pending wrap-up not cleared", channel, exc_info=True)
        from kiro_crew.slack.code_channels.cleanup import KEPT, release_channel_workspace

        outcome, reason = await release_channel_workspace(binding, caller=user)
        try:
            if not binding.wrapped_up_at:
                await asyncio.to_thread(_mark_wrapped, channel, now)
        except Exception:
            logger.exception("code channel %s: archive bookkeeping failed", channel)
        surface.push_slots(surface.dashboard_state())
        if outcome == KEPT:
            from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

            kept = await asyncio.to_thread(WorkspaceStore().get, binding.team_id, channel)
            _notify_kept(binding, kept.worktree if kept else "", reason)
    return True

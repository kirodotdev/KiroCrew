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
import secrets
import time
from dataclasses import dataclass, replace
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.models import CodeChannelApiError
from kiro_crew.slack.code_channels.store import Binding, store
from kiro_crew.slack.code_channels.surface_store import get_async, retire_async, update_async

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
#: The live /wrap-up request per channel: the tag its prompt carries.
#:
#: The one invariant the fallback rests on: the facts-only wrap-up posts only at
#: the end of the turn whose OWN prompt carries the channel's live tag. Not "a
#: later turn", not "the queue looks empty": an ordinary turn queued ahead, a
#: prompt still in delivery, a discarded or steered prompt, a second /wrap-up
#: replacing the first, or a restart that forgot the tag all fail that test, so
#: each one posts nothing rather than a wrap-up nobody asked for.
_requests: dict[str, str] = {}
#: The tag the RUNNING turn's prompt carries, per channel (set at its start).
_carrying: dict[str, str] = {}


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
        return await asyncio.to_thread(_gather_facts_sync, binding)
    except Exception:
        return Facts()


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
    return lines


def _redact(text: str) -> str:
    """Agent-authored text for Slack, through the shared outbound display sink
    (:func:`.surface.display_text`, the one place a code channel calls it)."""
    return surface.display_text(text)


def _redact_payload(text: str) -> str:
    """:func:`_redact` for text Slack never parses as a message: an HTML view's
    document, a context-bar url or key. The same sink with no mention defang,
    which on these surfaces would only break ``@media`` rules, ``<!DOCTYPE>`` and
    ``/@scope/`` package links."""
    return surface.display_text(text, mentions=False)


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


UNRECORDED_NOTE = "This wrap-up wasn't saved, so it can't archive the channel. Run /wrap-up again."


async def _retire_unrecorded(slack: Any, channel: str, ts: str, blocks: list[dict]) -> None:
    """Take the Archive button off a posted wrap-up whose ts could not be stored.

    Its click would only ever answer "out of date", so the message says why
    instead. Not after a Stop: then the button stays and answers that itself.
    """
    if not surface.commit_if_not_stopped(channel):
        return
    try:
        await asyncio.shield(
            slack.update_message(
                channel, ts, "Wrap-up", blocks=_retired_blocks(blocks, UNRECORDED_NOTE)
            )
        )
    except Exception:
        logger.warning("code channel %s: unsaved wrap-up not retired", channel, exc_info=True)


# ── Posting ───────────────────────────────────────────────────────────────


def new_request_tag() -> str:
    """A tag for one /wrap-up prompt: the turn whose prompt carries it is its turn."""
    return f"wrap-up request {secrets.token_hex(4)}"


async def request_wrap_up(channel_id: str, user_id: str, tag: str | None = None) -> str:
    """``/wrap-up`` asked the agent for a summary; its own turn's end checks it arrived.

    Returns the tag the caller puts in the prompt. Live in memory before the
    store write, so no ordering of turns around the write can matter: only the
    turn that carries the tag can ever take the fallback (see ``_requests``).
    """
    tag = tag or new_request_tag()
    previous = _requests.get(channel_id)
    _requests[channel_id] = tag  # replaces an older request: only the newest is live
    try:
        await update_async(
            channel_id, wrapup_pending_since=time.time(), wrapup_requested_by=user_id
        )
    except BaseException:
        # Not armed after all: the request it replaced (if any) is live again.
        if _requests.get(channel_id) == tag:
            if previous is None:
                _requests.pop(channel_id, None)
            else:
                _requests[channel_id] = previous
        raise
    return tag


def carried_request(channel_id: str) -> str | None:
    """The /wrap-up tag the channel's running turn carries (``None`` in an ordinary turn)."""
    return _carrying.get(channel_id)


def _forget_request(channel_id: str, tag: str | None = None) -> bool:
    """Drop the live request (only *tag*'s, when given); True when it was live."""
    if tag is not None and _requests.get(channel_id) != tag:
        return False
    return _requests.pop(channel_id, None) is not None


async def cancel_wrap_up(channel_id: str, tag: str | None = None) -> None:
    """The ``/wrap-up`` prompt never reached the session: nothing to fall back for.

    With *tag*, only that request is cancelled: a newer /wrap-up in the same
    channel stays live. Forgotten in memory first, so a failed write still
    posts no stray wrap-up.
    """
    if tag is not None and _requests.get(channel_id) not in (tag, None):
        return  # a newer request owns the channel's flag now
    _forget_request(channel_id, tag)
    await update_async(channel_id, wrapup_pending_since=0.0, wrapup_requested_by="")


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


async def post_wrap_up(
    binding: Binding,
    summary: str,
    *,
    caller: str,
    settles: str | None = None,
    only_if_live: bool = False,
) -> str:
    """Post the wrap-up in *binding*'s channel; returns its ts ("" when it failed).

    *settles* is the /wrap-up request this post answers (``None``: none, e.g.
    the agent wrapped up in an ordinary turn), and only that one is settled.
    *only_if_live* (the facts-only fallback) posts nothing unless that request
    is still the live one when the channel's lock is held: a newer /wrap-up
    landing meanwhile replaces it.

    Admitted at the stop fence (kept from an outer admission, e.g. the tool
    call): a Stop landing anywhere below posts nothing.
    """
    token = surface.admit()
    try:
        return await _post_wrap_up(
            binding, summary, caller=caller, settles=settles, only_if_live=only_if_live
        )
    finally:
        surface.release(token)


async def _post_wrap_up(
    binding: Binding,
    summary: str,
    *,
    caller: str,
    settles: str | None,
    only_if_live: bool,
) -> str:
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
    if not surface.channel_admits(live_config(), binding):
        surface.audit(
            caller,
            "slack.code_channel.wrap_up",
            "denied",
            f"channel={channel}",
            "channel or origin does not admit the bot",
        )
        return ""
    if surface.channel_policy_refusal(
        channel, operation="slack.code_channel.wrap_up", caller=caller
    ):
        return ""
    facts = await gather_facts(binding)
    if surface.channel_stopped(channel):
        # Slack's Stop landed while the facts were read: post nothing.
        surface.audit(
            caller, "slack.code_channel.wrap_up", "denied", f"channel={channel}", "stopped"
        )
        return ""
    blocks = wrap_up_blocks(summary, facts, channel)
    fallback = "Wrap-up: " + _redact(summary.strip() or DEFAULT_SUMMARY)[:200]
    async with _lock(channel):
        if await surface.is_archived(binding):
            surface.audit(
                caller, "slack.code_channel.wrap_up", "denied", f"channel={channel}", "archived"
            )
            return ""
        if only_if_live and (settles is None or _requests.get(channel) != settles):
            return ""  # replaced or settled while the facts were read: not ours to post
        if surface.channel_policy_refusal(
            channel, operation="slack.code_channel.wrap_up", caller=caller
        ):
            return ""  # again at the commit point, after the facts and the lock
        if not surface.commit_if_not_stopped(channel):
            # A Stop landed during the lock wait: post nothing (no await since).
            surface.audit(
                caller, "slack.code_channel.wrap_up", "denied", f"channel={channel}", "stopped"
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
        # A wrap-up is up: it settles the request it answers (only that one),
        # whatever happens to the bookkeeping below.
        if settles is not None:
            _forget_request(channel, settles)
        now = time.time()
        # Shielded: once the message is up, a cancel must not stop its ts being
        # stored, or its live Archive button would answer "out of date" forever.
        # The pending flag is cleared only when no newer /wrap-up is still live.
        settled: dict[str, Any] = (
            {} if channel in _requests else {"wrapup_pending_since": 0.0, "wrapup_requested_by": ""}
        )
        recorded = asyncio.ensure_future(
            update_async(channel, summary_ts=ts, summary_session_key=binding.session_key, **settled)
        )
        cancelled = False
        try:
            await asyncio.shield(recorded)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass  # read off the future below
        while not recorded.done():  # finish the write, then let the cancel carry on
            try:
                await asyncio.wait({recorded})
            except asyncio.CancelledError:
                cancelled = True
        if recorded.cancelled() or recorded.exception() is not None:
            logger.warning(
                "code channel %s: wrap-up ts not stored",
                channel,
                exc_info=None if recorded.cancelled() else recorded.exception(),
            )
            await _retire_unrecorded(slack, channel, ts, blocks)
            surface.audit(
                caller, "slack.code_channel.wrap_up", "error", f"channel={channel}", "not stored"
            )
            if cancelled:
                raise asyncio.CancelledError
            return ""
        await asyncio.to_thread(_mark_wrapped, channel, now)
        surface.push_slots(surface.dashboard_state())
        if cancelled:
            raise asyncio.CancelledError
    client = surface.client()
    if client is not None:
        try:
            await client.set_summary_message(channel, ts)
        except CodeChannelApiError as exc:
            logger.info("code channel %s: summary_message not recorded: %s", channel, exc.code)
    surface.audit(caller, "slack.code_channel.wrap_up", "allowed", f"channel={channel} ts={ts}")
    return ts


async def on_turn_start(binding: Binding, event: Any) -> None:
    """Note whether this turn's own prompt is the live /wrap-up request's."""
    channel = binding.channel_id
    tag = _requests.get(channel)
    prompt = str(getattr(event, "prompt", "") or "")
    if tag and tag in prompt:
        _carrying[channel] = tag
    else:
        _carrying.pop(channel, None)


async def on_turn_end(binding: Binding, event: Any) -> None:
    """The /wrap-up turn ended without the summary: post the facts.

    Only that turn: one whose prompt carried the live request's tag.
    """
    channel = binding.channel_id
    tag = _carrying.pop(channel, None)
    if not tag or _requests.get(channel) != tag:
        return  # not the /wrap-up turn (or that request was replaced or settled)
    record = await get_async(channel)
    if record is None or not record.wrapup_pending_since or await surface.is_archived(binding):
        _forget_request(channel, tag)
        return
    if getattr(event, "outcome", "ok") != "ok" or surface.channel_stopped(channel):
        await cancel_wrap_up(channel, tag)
        return
    await post_wrap_up(
        binding,
        DEFAULT_SUMMARY,
        caller=record.wrapup_requested_by or "slack",
        settles=tag,
        only_if_live=True,
    )


# ── Archive ───────────────────────────────────────────────────────────────

ALREADY_ARCHIVED_TEXT = "This channel is already archived."
STALE_TEXT = "That wrap-up is out of date. Run `/wrap-up` for a fresh one."
NOT_ALLOWED_TEXT = "⛔ You can't archive this channel."
ARCHIVE_FAILED_TEXT = "I couldn't archive this channel ({code}). Try again in a moment."


async def _ephemeral(channel: str, user: str, text: str) -> None:
    slack = surface.slack()
    if slack is None or not (channel and user) or not surface.commit_if_not_stopped(channel):
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


def _notify_unrecorded(binding: Binding, path: str, *, final: bool = False) -> None:
    state = surface.dashboard_state()
    notify = getattr(state, "notify", None)
    where = f"the worktree at {path}" if path else "its worktree"
    if final:
        body = (
            "Slack archived the channel but Kiro Crew could not record it, so nothing "
            f"was cleaned up: {where} stays in place; remove it by hand."
        )
    else:
        body = (
            "Slack archived the channel but Kiro Crew could not record it yet, so "
            "nothing is cleaned up until it does; it is retrying now. If the gateway "
            f"restarts first, {where} stays in place; remove it by hand."
        )
    if callable(notify):
        try:
            notify(
                "slack",
                "Code channel archived; not recorded",
                body,
                meta={"channel_id": binding.channel_id, "session_key": binding.session_key},
            )
        except Exception:
            logger.debug("unrecorded-archive notification failed", exc_info=True)


#: Waits before each background attempt to record an unrecorded archive.
_RECORD_RETRY_DELAYS: tuple[float, ...] = (2.0, 10.0, 30.0)
_recoveries: dict[str, asyncio.Task[None]] = {}


async def _worktree_path(binding: Binding) -> str:
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    try:
        record = await asyncio.to_thread(WorkspaceStore().get, binding.team_id, binding.channel_id)
    except Exception:
        return ""
    return str(record.worktree) if record is not None else ""


async def _still_archived(channel: str) -> bool | None:
    """Slack's own answer: archived, unarchived (False), or unknown (None)."""
    client = surface.client()
    if client is None:
        return None
    try:
        return await client.archived_in_slack(channel)
    except Exception:
        logger.info("code channel %s: archived state unreadable", channel, exc_info=True)
        return None


async def _recover_archive(
    binding: Binding, user: str, now: float, resources: str, path: str
) -> None:
    """Record an archive Slack already made, then clean up as the click would have.

    Each attempt first asks Slack whether the channel is still archived, under
    the channel's lock: a channel unarchived meanwhile is unfenced and left
    alone, never stamped or cleaned up.
    """
    channel = binding.channel_id
    operation = "slack.code_channel.archive"
    for delay in _RECORD_RETRY_DELAYS:
        await asyncio.sleep(delay)
        async with _lock(channel):
            try:
                current = await asyncio.to_thread(store().get_binding, channel)
            except Exception:
                logger.warning("code channel %s: binding unreadable", channel, exc_info=True)
                continue
            if current is None or current.session_key != binding.session_key:
                surface.unfence(channel)
                surface.audit(user, operation, "denied", resources, "binding replaced")
                return
            archived = await _still_archived(channel)
            if archived is False:
                # A stamp the click wrote before handing over is taken back; a
                # failed write is retried rather than left on a live channel.
                if current.archived_at and not await _stamp_archived(channel, 0.0, attempts=1):
                    continue
                surface.unfence(channel)
                surface.audit(user, operation, "allowed", f"{resources} result=unarchived")
                return
            if archived is None or not await _stamp_archived(channel, now, attempts=1):
                continue
            # An unarchive landing between the check and the stamp would be
            # overwritten by it: ask again, and take the stamp back if so.
            again = await _still_archived(channel)
            if again is False:
                if not await _stamp_archived(channel, 0.0, attempts=1):
                    continue  # the next attempt sees it live and takes the stamp back
                surface.unfence(channel)
                surface.audit(user, operation, "allowed", f"{resources} result=unarchived")
                return
            if again is None:
                continue  # can't tell (e.g. the policy now refuses the read): remove nothing
            await _finish_archive(current, channel, user, now, resources)
            return
    # Still fenced: no tool, command or wrap-up acts on the channel here.
    surface.audit(
        user,
        operation,
        "error",
        f"{resources} result=archived",
        "archive not recorded after retries",
    )
    _notify_unrecorded(binding, path, final=True)


def _start_recovery(binding: Binding, user: str, now: float, resources: str, path: str) -> None:
    """One background recovery per channel (a running one is left alone)."""
    channel = binding.channel_id
    running = _recoveries.get(channel)
    if running is not None and not running.done():
        return
    task = asyncio.ensure_future(_recover_archive(binding, user, now, resources, path))
    _recoveries[channel] = task

    def _done(finished: asyncio.Task[None]) -> None:
        if _recoveries.get(channel) is finished:
            del _recoveries[channel]
        if not finished.cancelled() and finished.exception() is not None:
            logger.warning(
                "code channel %s: archive recovery failed",
                channel,
                exc_info=finished.exception(),
            )

    task.add_done_callback(_done)


async def drain_recoveries() -> None:
    """Wait for background archive recoveries (tests, shutdown)."""
    while _recoveries:
        await asyncio.gather(*list(_recoveries.values()), return_exceptions=True)


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


def _notify_branch_kept(binding: Binding, reason: str) -> None:
    """The worktree is gone but its branch stays (*reason* names it and why)."""
    state = surface.dashboard_state()
    notify = getattr(state, "notify", None)
    if not callable(notify):
        return
    try:
        notify(
            "slack",
            "Code channel archived; branch kept",
            f"The worktree was removed, but the {reason}.",
            meta={"channel_id": binding.channel_id, "session_key": binding.session_key},
        )
    except Exception:
        logger.debug("branch-kept notification failed", exc_info=True)


async def handle_archive_click(orch: Any, payload: dict[str, Any]) -> bool:
    """An Archive click; False when it is not ours to answer (the gate is shut)."""
    if not code_channels_on(live_config(orch)):
        return False
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit
    from kiro_crew.slack.handler import is_allowed_user

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
    if surface.channel_policy_refusal(channel, operation=operation, caller=user):
        # The per-channel ceiling, here too: whoever calls this, no archive and
        # no worktree removal in a channel the policy refuses (audited there).
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    async with _lock(channel):
        binding = await asyncio.to_thread(store().get_binding, channel) or binding
        try:
            record = await get_async(channel)
        except Exception:
            # Unreadable surface file: no way to tell this button is current.
            logger.warning("code channel %s: surface record unreadable", channel, exc_info=True)
            surface.audit(user, operation, "error", resources, "surface record unreadable")
            await _ephemeral(channel, user, ARCHIVE_FAILED_TEXT.format(code="state unreadable"))
            return True
        stale = (
            record is None
            or not record.summary_ts
            or record.summary_ts != msg_ts
            or record.summary_session_key != binding.session_key
        )
        if not stale and await surface.fenced_unrecorded(binding):
            # Archived here but never recorded: record it now rather than
            # answer "already archived" with nothing cleaned up.
            if surface.channel_policy_refusal(channel, operation=operation, caller=user):
                # The recovery stamps and removes the worktree: same ceiling.
                await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
                return True
            surface.audit(user, operation, "allowed", f"{resources} result=archive_retry")
            _start_recovery(binding, user, time.time(), resources, await _worktree_path(binding))
            await _ephemeral(channel, user, ALREADY_ARCHIVED_TEXT)
            return True
        if await surface.is_archived(binding):
            surface.audit(user, operation, "allowed", f"{resources} result=already_archived")
            await _ephemeral(channel, user, ALREADY_ARCHIVED_TEXT)
            return True
        if stale:
            surface.audit(user, operation, "denied", resources, "not the current wrap-up")
            await _ephemeral(channel, user, STALE_TEXT)
            return True
        if surface.channel_policy_refusal(channel, operation=operation, caller=user):
            # Again at the commit point, after the awaits above: no button
            # retired, no archive and no worktree removal once it refuses.
            await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
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
            # A lost answer may hide an archive Slack applied: unless Slack says
            # the channel is live, go on as archived; the path below re-checks
            # and leaves a "can't tell" to the bounded recovery.
            lost_but_applied = exc.maybe_applied and await _still_archived(channel) is not False
            if lost_but_applied:
                surface.audit(
                    user, operation, "error", resources, f"{exc.code}: answer lost, reconciling"
                )
            if exc.code != "already_archived" and not lost_but_applied:
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
            # Archived in Slack but not recorded: the fence stays, and a bounded
            # background retry records it and then cleans up as below. A gateway
            # restart before it succeeds leaves the worktree for removal by hand.
            surface.audit(
                user, operation, "error", f"{resources} result=archived", "archive not recorded"
            )
            path = await _worktree_path(binding)
            _notify_unrecorded(binding, path)
            _start_recovery(binding, user, now, resources, path)
            return True
        # An unarchive recorded between Slack's archive and the stamp above was
        # just overwritten by it: ask Slack again, as the recovery does, and take
        # the stamp back if the channel is live.
        again = await _still_archived(channel)
        if again is False and await _stamp_archived(channel, 0.0, attempts=1):
            surface.unfence(channel)
            surface.audit(user, operation, "allowed", f"{resources} result=unarchived")
            return True
        if again is not True:
            # Can't tell, or live but the stamp could not be taken back: remove
            # nothing now. The fence stays and the bounded background retry asks
            # again, taking the stamp back or cleaning up as Slack then says.
            surface.audit(user, operation, "allowed", f"{resources} result=archive_retry")
            _start_recovery(binding, user, now, resources, await _worktree_path(binding))
            return True
        await _finish_archive(binding, channel, user, now, resources)
    return True


async def _finish_archive(
    binding: Binding, channel: str, user: str, now: float, resources: str = ""
) -> None:
    """The archive is recorded: unfence, audit, and release the worktree."""
    operation = "slack.code_channel.archive"
    resources = resources or f"channel={channel}"
    surface.unfence(channel)
    surface.audit(user, operation, "allowed", f"{resources} result=archived")
    from kiro_crew.slack.code_channels import status, tools

    status.hold_after_turn(channel, None)  # an archived channel takes no further turn
    tools.forget_channel(channel)
    try:
        await cancel_wrap_up(channel)
    except Exception:
        logger.warning("code channel %s: pending wrap-up not cleared", channel, exc_info=True)
    _forget_request(channel)  # also when the write above failed
    _carrying.pop(channel, None)
    from kiro_crew.slack.code_channels.cleanup import KEPT, release_channel_workspace

    outcome, reason = await release_channel_workspace(binding, caller=user)
    try:
        if not binding.wrapped_up_at:
            await asyncio.to_thread(_mark_wrapped, channel, now)
    except Exception:
        logger.exception("code channel %s: archive bookkeeping failed", channel)
    try:
        # Archived: its surface record goes first past the cap (kept for an unarchive).
        await retire_async(channel)
    except Exception:
        logger.warning("code channel %s: surface record not retired", channel, exc_info=True)
    surface.push_slots(surface.dashboard_state())
    if outcome == KEPT:
        _notify_kept(binding, await _worktree_path(binding), reason)
    elif reason:
        _notify_branch_kept(binding, reason)

"""Publish the work board: one pinned message per channel, edited in place.

Called by the workspace refresh (:meth:`~..workspace.service.WorkspaceService.refresh`)
on its snapshot, outside the channel's lock, so the board, the context bar and the
Code-tab diff are published from the same snapshot by the same per-channel worker.
Publishes of one channel are kept apart by that worker (``schedule_refresh``), not
by the lock -- which matters, because posting a missing board and paging the owner
are not idempotent. That worker is
already coalescing (one run plus at most one queued rerun per channel) and the
board's triggers arm it through a short debounce window, so a burst of worker
reports costs one ``chat.update``.

* **First publish** posts the board and pins it (``pins.add``; ``already_pinned``
  counts as pinned). The message ts, a digest of what it shows, and the pin
  are kept on the channel's surface record.
* **Later publishes** edit that message with ``chat.update`` only when the digest
  moved. A message Slack does not have any more (``message_not_found``,
  ``cant_update_message``) is reposted and pinned again.
* **Waiting on you.** A worker that newly waits on a person (a report with
  ``reason`` ``needs_human`` / ``approval``, or a tool approval pending on its
  session) gets ONE threaded reply under the board that mentions the owner. Sent
  once per report (``<item_id>:<last_report_at>``) and once per spell of a
  pending approval, so the board's own edits never page anyone twice.

Every write first checks the ``channels`` policy and the archive / Stop fences the
rest of the surface honours; nothing here raises into a turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.board import model, render
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

#: Needs-you alert tokens remembered per channel.
MAX_ALERTS = 100
#: Slack answers meaning the board message is gone: post a new one.
_GONE = frozenset({"message_not_found", "cant_update_message", "edit_window_closed"})
#: Slack answers meaning the plan block itself was refused: fall back to the rows.
_PLAN_REFUSED = frozenset({"invalid_blocks", "invalid_blocks_format", "feature_disabled"})
#: Channels whose Slack refused the plan block (in this process; retried after a restart).
_plan_refused: set[str] = set()
#: Answers no retry cures: the board is not retried on a timer after one of these.
_PERMANENT = frozenset(
    {
        "channel_not_found",
        "is_archived",
        "is_inactive",
        "not_in_channel",
        "restricted_action",
        "not_authed",
        "invalid_auth",
        "account_inactive",
        "missing_scope",
    }
)


def _code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    try:
        return str((response or {}).get("error", "") or "")
    except Exception:
        return ""


def digest(snapshot: model.BoardSnapshot, links: dict[str, str], look: str = "plan") -> str:
    """What the board SHOWS, hashed: its rendered blocks, without the "Updated" stamp.

    Hashing the render rather than the snapshot keeps the digest honest for any
    look: a row field the current renderer does not show never forces an edit,
    and one it does always does.
    """
    blocks = render.board_blocks(
        snapshot,
        lambda key: links.get(key, ""),
        conductor_link=links.get("@conductor", ""),
        updated="",
        look=look,
    )
    payload = {"blocks": blocks, "text": render.fallback_text(snapshot)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def alert_token(row: model.WorkerRow) -> str:
    """Once per report that waits on a person; once per spell of a pending approval."""
    if row.waiting_on == "approval":
        return f"{row.item_id}:approval:{row.report_stamp}"
    return f"{row.item_id}:{row.report_stamp or 'live'}"


def _is_approval_alert(key: str) -> bool:
    return ":approval:" in key


def _stamp() -> str:
    return time.strftime("%H:%M UTC", time.gmtime())


async def publish(
    binding: Binding,
    snapshot: model.BoardSnapshot | None,
    *,
    link_for: Callable[[str], str],
    owner_id: str = "",
) -> str:
    """Bring the channel's board up to *snapshot*; returns what it did (for tests/logs).

    ``""`` nothing to do, ``posted`` / ``updated`` / ``reposted``, or a refusal
    word (``no_slack``, ``denied``, ``archived``, ``stopped``, ``failed``).
    """
    if snapshot is None:
        return ""
    slack = surface.slack()
    if slack is None:
        return "no_slack"
    channel = binding.channel_id
    if await surface.is_archived(binding):
        return "archived"
    if surface.channel_stopped(channel):
        return "stopped"
    if not await surface.outbound_permitted():
        surface.audit(
            "slack", "slack.code_channel.board", "denied", f"channel={channel}", "channels policy"
        )
        return "denied"
    links = {r.session_key: link_for(r.session_key) for r in snapshot.rows if r.session_key}
    conductor_link = link_for(binding.session_key) if binding.session_key else ""
    links["@conductor"] = conductor_link

    def _link(key: str) -> str:
        return links.get(key, "")

    record = await get_async(channel)
    ts = record.board_ts if record else ""
    pinned = bool(record.board_pinned) if record else False
    done = ""
    now = _clock()
    look = "rows" if channel in _plan_refused else "plan"
    new_digest = digest(snapshot, links, look)
    changed = not ts or record is None or record.board_digest != new_digest
    active = any(row.state in model.ACTIVE_STATES for row in snapshot.rows)
    last = _last_edit.get(channel)
    # A live board whose "Updated" stamp went stale is edited even unchanged.
    stale = bool(ts) and active and (last is None or now - last >= STALE_SECS)
    if ts and (changed or stale) and last is not None and now - last < MIN_EDIT_GAP_SECS:
        # A busy board is edited at most once per gap: the latest state then.
        _retry_at[channel] = MIN_EDIT_GAP_SECS - (now - last)
        changed = stale = False
        done = "deferred"
    if changed or stale:
        text = render.fallback_text(snapshot)
        while True:
            # A new block_id on every edit: Slack requires it of a plan block.
            blocks = render.board_blocks(
                snapshot,
                _link,
                conductor_link=conductor_link,
                updated=_stamp(),
                look=look,
                block_seed=uuid.uuid4().hex[:12],
            )
            try:
                if ts:
                    await slack.update_message(channel, ts, text, blocks)
                    done = "updated"
                else:
                    ts = str(await slack.post_blocks(channel, blocks, text) or "")
                break
            except Exception as exc:
                code = _code(exc)
                if look == "plan" and code in _PLAN_REFUSED:
                    # This client or workspace takes no plan block: the mrkdwn
                    # rows from now on, sent at once, so the board never goes stale.
                    _plan_refused.add(channel)
                    logger.warning(
                        "code channel %s: Slack refused the plan block (%s); "
                        "the board falls back to plain rows",
                        channel,
                        code,
                    )
                    look = "rows"
                    new_digest = digest(snapshot, links, look)
                    continue
                if ts and code in _GONE:
                    ts, pinned = "", False
                    continue
                return _failed(channel, "update" if ts else "post", exc)
        if not ts or done != "updated":
            if not ts:
                return _failed(channel, "post", None)
            done = "reposted" if record and record.board_ts else "posted"
            surface.audit(
                "slack", "slack.code_channel.board", "allowed", f"channel={channel} ts={ts}"
            )
        _succeeded(channel, now)
        try:
            await update_async(channel, board_ts=ts, board_digest=new_digest, board_pinned=pinned)
        except Exception:
            logger.warning("code channel %s: board pointer not stored", channel, exc_info=True)
    if ts and pinned and active and now - _pin_checked.get(channel, 0.0) >= REPIN_SECS:
        # Someone may have unpinned it; pinning again is idempotent (already_pinned).
        pinned = False
    if ts and not pinned:
        _pin_checked[channel] = now
        pinned = await _pin(slack, channel, ts)
        if pinned:
            try:
                await update_async(channel, board_pinned=True)
            except Exception:
                logger.debug("code channel %s: pin flag not stored", channel, exc_info=True)
    if ts:
        await _alert(slack, channel, ts, snapshot, _link, owner_id)
    if ts and active:
        # The stamp's own deadline, from the last EDIT: the caller re-arms the
        # refresh for then, so a live board that nothing else moves is still
        # re-stamped about once a minute (a sooner gap or retry wins).
        since = now - _last_edit.get(channel, now)
        _retry_at.setdefault(channel, max(1.0, STALE_SECS - since))
    return done


#: Shortest gap between two edits of one board (Slack's ``chat.update`` is Tier 3,
#: 50+ a minute per workspace; one board edits at most ~3 a minute).
MIN_EDIT_GAP_SECS = 20.0
#: A working or waiting board is edited at least this often, so its "Updated"
#: stamp is never older than about a minute.
STALE_SECS = 60.0
#: How often a live board's pin is put back (someone may have unpinned it).
REPIN_SECS = 600.0
#: Backoff for a failed edit or post: first, then x3 per failure, capped.
_RETRY_FIRST_SECS = 5.0
_RETRY_CAP_SECS = 300.0
_MAX_TRACKED = 4096
#: The publisher's clock (monotonic seconds); tests hand it a fake.
_clock: Callable[[], float] = time.monotonic

#: Per channel, in this process: the last successful edit or post (monotonic),
#: the failures since, the last pin check, and a retry the caller should arm.
_last_edit: dict[str, float] = {}
_failures: dict[str, int] = {}
_pin_checked: dict[str, float] = {}
_retry_at: dict[str, float] = {}


def take_retry(channel: str) -> float:
    """The delay a retry of *channel*'s publish should wait, once (``0``: none)."""
    return _retry_at.pop(channel, 0.0)


def _retry_after(exc: BaseException | None) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        return float(headers.get("Retry-After") or headers.get("retry-after") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _failed(channel: str, what: str, exc: BaseException | None) -> str:
    """Count a failed edit or post and arm its retry; logged once per failure spell.

    A refusal no retry can cure (the channel is gone, archived, or the app is out
    of it) arms nothing: the next trigger tries again, and the archive path
    forgets the channel.
    """
    code = (_code(exc) if exc is not None else "") or (str(exc) if exc else "no ts")
    if code in _PERMANENT:
        if _failures.get(channel, 0) == 0:
            logger.warning("code channel %s: board %s refused (%s)", channel, what, code)
        _failures[channel] = _failures.get(channel, 0) + 1
        _retry_at.pop(channel, None)
        return "failed"
    if len(_failures) > _MAX_TRACKED:
        _failures.clear()
    count = _failures.get(channel, 0) + 1
    _failures[channel] = count
    backoff = min(_RETRY_FIRST_SECS * 3 ** (count - 1), _RETRY_CAP_SECS)
    _retry_at[channel] = max(backoff, _retry_after(exc))
    if count == 1:
        logger.warning("code channel %s: board %s failed (%s); retrying", channel, what, code)
    else:
        logger.debug("code channel %s: board %s failed again (%s)", channel, what, code)
    return "failed"


def _succeeded(channel: str, now: float) -> None:
    if len(_last_edit) > _MAX_TRACKED:
        _last_edit.clear()
    _last_edit[channel] = now
    if _failures.pop(channel, 0):
        logger.info("code channel %s: board published again", channel)


def forget(channel: str) -> None:
    """Drop the per-channel publish state (the channel was archived or wrapped up)."""
    for table in (_last_edit, _failures, _pin_checked, _retry_at):
        table.pop(channel, None)
    _plan_refused.discard(channel)


async def _pin(slack: Any, channel: str, ts: str) -> bool:
    try:
        await slack.add_pin(channel, ts)
        return True
    except Exception as exc:
        if _code(exc) == "already_pinned":
            return True
        logger.info("code channel %s: board not pinned: %s", channel, _code(exc) or exc)
        return False


async def _alert(
    slack: Any,
    channel: str,
    board_ts: str,
    snapshot: model.BoardSnapshot,
    link_for: Callable[[str], str],
    owner_id: str,
) -> None:
    """Thread one owner mention per worker report that waits on a person."""
    waiting = [row for row in snapshot.rows if row.needs_owner]
    record = await get_async(channel)
    sent = list(record.board_alerts) if record else []
    # An approval alert lives only while that approval is pending: once it is
    # answered it is forgotten, so the NEXT approval pages again.
    live = {alert_token(row) for row in waiting}
    kept = [t for t in sent if not _is_approval_alert(t) or t in live]
    cleared = len(kept) != len(sent)
    sent = kept
    fresh = [row for row in waiting if alert_token(row) not in sent]
    for row in fresh:
        try:
            await slack.post_message(
                channel, render.needs_you_text(row, owner_id, link_for), thread_ts=board_ts
            )
        except Exception as exc:
            logger.info("code channel %s: needs-you reply failed: %s", channel, _code(exc) or exc)
            continue
        sent.append(alert_token(row))
        surface.audit(
            "slack",
            "slack.code_channel.board_alert",
            "allowed",
            f"channel={channel} item={row.item_id}",
        )
    if fresh or cleared:
        try:
            await update_async(channel, board_alerts=sent[-MAX_ALERTS:])
        except Exception:
            logger.debug("code channel %s: alert tokens not stored", channel, exc_info=True)

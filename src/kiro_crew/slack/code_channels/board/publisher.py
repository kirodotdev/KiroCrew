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
  are kept on the channel's surface record, with the message's permalink (read
  once per message) that the context bar's Workers item links to. The pin needs
  :data:`PIN_SCOPE`, which the manifest's code-channels group asks for; a pin
  Slack refuses (an install that has not re-approved it yet) is tried again every
  :data:`REPIN_SECS`, not on each publish, and at once once the scope is granted.
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

from kiro_crew.slack import features
from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.board import model, render
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import get_async, update_async
from kiro_crew.slack.retry import _retry_after_secs, is_retryable_slack_error

logger = logging.getLogger(__name__)

#: Needs-you alert tokens remembered per channel.
MAX_ALERTS = 100
#: Slack answers meaning the board message is gone: post a new one.
_GONE = frozenset({"message_not_found", "cant_update_message", "edit_window_closed"})
#: Slack answers meaning the plan block itself was refused: fall back to the rows.
_PLAN_REFUSED = frozenset({"invalid_blocks", "invalid_blocks_format", "feature_disabled"})
#: Channels whose Slack refused the plan block (in this process; retried after a restart).
_plan_refused: set[str] = set()
#: Channels whose Slack refused the board's tab links (in this process).
_tabs_refused: set[str] = set()


def _code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    try:
        return str((response or {}).get("error", "") or "")
    except Exception:
        return ""


def digest(
    snapshot: model.BoardSnapshot,
    links: dict[str, str],
    look: str = "plan",
    tabs: render.Tabs | None = None,
) -> str:
    """What the board SHOWS, hashed: its rendered blocks, without the "Updated" stamp.

    Hashing the render rather than the snapshot keeps the digest honest for any
    look: a row field the current renderer does not show never forces an edit,
    and one it does always does -- a tab link appearing included.
    """
    blocks = render.board_blocks(
        snapshot,
        lambda key: links.get(key, ""),
        conductor_link=links.get("@conductor", ""),
        updated="",
        look=look,
        tabs=tabs,
    )
    payload = {"blocks": blocks, "text": render.fallback_text(snapshot)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def alert_token(row: model.WorkerRow) -> str:
    """Once per report that waits on a person; once per pending approval.

    An approval's token carries that approval's own key, so a second approval
    asked under the same report (inside one debounce, with no report between)
    pages again instead of matching the first one's retained token.
    """
    if row.waiting_on == "approval":
        return f"{row.item_id}:approval:{row.report_stamp}:{row.approval_key}"
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
    tabs: render.Tabs | None = None,
    urgent: bool = False,
    admits: Callable[[], bool] | None = None,
) -> str:
    """Bring the channel's board up to *snapshot*; returns what it did (for tests/logs).

    *admits* is the channel's scope check, asked again right before every Slack
    write (each post or edit, each retry of one, each needs-you reply), since the
    awaits in between can outlast a change of the channel's sharing or activation.

    ``""`` nothing to do, ``posted`` / ``updated`` / ``reposted``, or a refusal
    word (``no_slack``, ``denied``, ``archived``, ``stopped``, ``failed``).
    *tabs* deep-links the channel's own tabs from the board; a board Slack refuses
    with them is sent again without them (and stays without them in this process)
    before the plan block itself is given up.
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
    ts = _unsaved_ts.get(channel) or (record.board_ts if record else "")
    pinned = bool(record.board_pinned) if record else False
    done = ""
    now = _clock()
    look = "rows" if channel in _plan_refused else "plan"
    if channel in _tabs_refused:
        tabs = None
    new_digest = digest(snapshot, links, look, tabs)
    changed = not ts or record is None or record.board_digest != new_digest
    active = any(row.state in model.ACTIVE_STATES for row in snapshot.rows)
    last = _last_edit.get(channel)
    # A live board whose "Updated" stamp went stale is edited even unchanged.
    stale = bool(ts) and active and (last is None or now - last >= STALE_SECS)
    if (
        ts
        and (changed or stale)
        and not urgent
        and last is not None
        and now - last < MIN_EDIT_GAP_SECS
    ):
        # A busy board is edited at most once per gap: the latest state then.
        _retry_at[channel] = MIN_EDIT_GAP_SECS - (now - last)
        changed = stale = False
        done = "deferred"
    if changed or stale:
        text = render.fallback_text(snapshot)
        dropped_tabs = False
        while True:
            if not _still(admits):
                return "out_of_scope"
            # A new block_id on every edit: Slack requires it of a plan block.
            blocks = render.board_blocks(
                snapshot,
                _link,
                conductor_link=conductor_link,
                updated=_stamp(),
                look=look,
                block_seed=uuid.uuid4().hex[:12],
                tabs=tabs,
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
                if look == "plan" and code in _PLAN_REFUSED and tabs is not None and tabs.any:
                    # The tab links first: the board itself must never go stale
                    # for want of a deep link. Remembered only once the plan
                    # without them lands, so a refusal of the plan block itself
                    # is not blamed on the links.
                    dropped_tabs = True
                    logger.warning(
                        "code channel %s: Slack refused the board with tab links (%s); "
                        "sending it without them",
                        channel,
                        code,
                    )
                    tabs = None
                    new_digest = digest(snapshot, links, look, tabs)
                    continue
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
                    new_digest = digest(snapshot, links, look, tabs)
                    continue
                if ts and code in _GONE:
                    ts, pinned = "", False
                    continue
                return _failed(channel, "update" if ts else "post", exc)
        if dropped_tabs and look == "plan":
            _tabs_refused.add(channel)
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
            _unsaved_ts.pop(channel, None)
        except Exception:
            # Slack holds this message: the next publish edits it, not a new one.
            if len(_unsaved_ts) > _MAX_TRACKED:
                _unsaved_ts.clear()
            _unsaved_ts[channel] = ts
            logger.warning("code channel %s: board pointer not stored", channel, exc_info=True)
    checked = _pin_checked.get(channel)
    pin_due = checked is None or now - checked >= REPIN_SECS or _pin_newly_granted(channel)
    if ts and pinned and active and pin_due:
        # Someone may have unpinned it; pinning again is idempotent (already_pinned).
        pinned = False
    # A board just posted is pinned at once. One whose pin failed (an install
    # that has not re-approved the manifest's ``pins:write``) is tried again
    # only every REPIN_SECS, not on each publish -- or at once when the scope
    # has been granted since.
    fresh = done in ("posted", "reposted")
    if ts and not pinned and (fresh or pin_due) and _still(admits):
        _pin_checked[channel] = now
        pinned = await _pin(slack, channel, ts)
        _note_pin(channel, pinned)
        if pinned:
            try:
                await update_async(channel, board_pinned=True)
            except Exception:
                logger.debug("code channel %s: pin flag not stored", channel, exc_info=True)
    if ts:
        await _record_link(slack, channel, ts)
        await _record_summary(binding, ts, admits)
        await _alert(slack, channel, ts, snapshot, _link, owner_id, admits)
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
#: 5 s x 3**4 = 405 s, already past the cap: no larger power is ever needed.
_RETRY_MAX_EXPONENT = 4
_MAX_TRACKED = 4096
#: The publisher's clock (monotonic seconds); tests hand it a fake.
_clock: Callable[[], float] = time.monotonic

#: Per channel, in this process: the last successful edit or post (monotonic),
#: the failures since, the last pin check, and a retry the caller should arm.
_last_edit: dict[str, float] = {}
_failures: dict[str, int] = {}
_pin_checked: dict[str, float] = {}
_retry_at: dict[str, float] = {}
#: The bot scope ``pins.add`` needs (the manifest's code-channels group asks for it).
PIN_SCOPE = "pins:write"
#: Channels whose last pin failed while :data:`PIN_SCOPE` was not granted: pinned
#: again at once when a re-approval grants it, not after REPIN_SECS.
_pin_refused_unscoped: set[str] = set()
#: Channels whose last board answer no retry cures (see :func:`_failed`).
_refused: set[str] = set()
#: What Slack holds but the surface store could not record (its write failed):
#: the board message's ts and the needs-you alert tokens already threaded. Read in
#: place of the store's older value until a write succeeds, so a failed write
#: never posts a second board or pages the owner again.
_unsaved_ts: dict[str, str] = {}
_unsaved_alerts: dict[str, list[str]] = {}


def _still(admits: Callable[[], bool] | None) -> bool:
    """The caller's scope check, asked again (fails closed)."""
    if admits is None:
        return True
    try:
        return bool(admits())
    except Exception:
        return False


def take_retry(channel: str) -> float:
    """The delay a retry of *channel*'s publish should wait, once (``0``: none)."""
    return _retry_at.pop(channel, 0.0)


def _failed(channel: str, what: str, exc: BaseException | None) -> str:
    """Count a failed edit or post and arm its retry; logged once per failure spell.

    Whether a retry can cure the failure is ``slack.retry``'s one rule
    (:func:`~kiro_crew.slack.retry.is_retryable_slack_error`: a rate limit, a 5xx
    or a transport blip is retried; Slack's own refusal -- the channel is gone,
    archived, the app is out of it -- is not), and a rate limit's wait is its
    ``Retry-After`` parser's. A refusal arms nothing: the next trigger tries
    again, and the archive path forgets the channel. A post Slack answered with
    no ts (*exc* ``None``) is retried.
    """
    code = (_code(exc) if exc is not None else "") or (str(exc) if exc else "no ts")
    if exc is not None and not is_retryable_slack_error(exc):
        if _failures.get(channel, 0) == 0:
            logger.warning("code channel %s: board %s refused (%s)", channel, what, code)
        _failures[channel] = _failures.get(channel, 0) + 1
        _retry_at.pop(channel, None)
        if len(_refused) > _MAX_TRACKED:
            _refused.clear()
        _refused.add(channel)
        return "failed"
    if len(_failures) > _MAX_TRACKED:
        _failures.clear()
    count = _failures.get(channel, 0) + 1
    _failures[channel] = count
    # The exponent is clamped before it is evaluated: the cap is reached by the
    # 5th failure, and an unbounded power overflows a float after ~650.
    backoff = min(_RETRY_FIRST_SECS * 3 ** min(count - 1, _RETRY_MAX_EXPONENT), _RETRY_CAP_SECS)
    _retry_at[channel] = max(backoff, (_retry_after_secs(exc) or 0.0) if exc else 0.0)
    if count == 1:
        logger.warning("code channel %s: board %s failed (%s); retrying", channel, what, code)
    else:
        logger.debug("code channel %s: board %s failed again (%s)", channel, what, code)
    return "failed"


def _succeeded(channel: str, now: float) -> None:
    if len(_last_edit) > _MAX_TRACKED:
        _last_edit.clear()
    _last_edit[channel] = now
    _refused.discard(channel)
    if _failures.pop(channel, 0):
        logger.info("code channel %s: board published again", channel)


def refused_permanently(channel: str) -> bool:
    """Whether Slack's last answer to *channel*'s board was one no retry cures."""
    return channel in _refused


def forget(channel: str) -> None:
    """Drop the per-channel publish state (the channel was archived, unbound or turned off)."""
    for table in (_last_edit, _failures, _pin_checked, _retry_at, _unsaved_ts, _unsaved_alerts):
        table.pop(channel, None)
    _plan_refused.discard(channel)
    _refused.discard(channel)
    _pin_refused_unscoped.discard(channel)
    _tabs_refused.discard(channel)


def _pin_scope_granted() -> bool:
    granted = features.granted_scopes()
    return granted is not None and PIN_SCOPE in granted


def _note_pin(channel: str, pinned: bool) -> None:
    """Remember a pin that failed while :data:`PIN_SCOPE` was not granted."""
    if pinned or _pin_scope_granted():
        _pin_refused_unscoped.discard(channel)
        return
    if len(_pin_refused_unscoped) > _MAX_TRACKED:
        _pin_refused_unscoped.clear()
    _pin_refused_unscoped.add(channel)


def _pin_newly_granted(channel: str) -> bool:
    """The pin failed without the scope, and the install has re-approved it since."""
    return channel in _pin_refused_unscoped and _pin_scope_granted()


async def _record_link(slack: Any, channel: str, board_ts: str) -> None:
    """Cache the board message's permalink: the context bar's Workers item links to it.

    Read with ``chat.getPermalink`` once per board message (an edit keeps the
    ts, so the link holds); a failed or unusable read is retried on the next publish.
    """
    record = await get_async(channel)
    if record is not None and record.board_link_ts == board_ts:
        return
    try:
        link = str(await slack.get_permalink(channel, board_ts) or "")
    except Exception as exc:
        logger.info("code channel %s: board permalink not read: %s", channel, _code(exc) or exc)
        return
    if not link.startswith("https://"):
        return  # nothing usable: asked again on the next publish
    try:
        await update_async(channel, board_link=link, board_link_ts=board_ts)
    except Exception:
        logger.debug("code channel %s: board permalink not stored", channel, exc_info=True)


async def _record_summary(
    binding: Binding, board_ts: str, admits: Callable[[], bool] | None = None
) -> None:
    """Make the board the channel's ``summary_message`` while the session runs.

    The board IS the conductor's running summary, so Slack can show it live
    (`code channel properties`_), not only after the archive. A current wrap-up
    (posted for this same session) is the summary instead and is never
    displaced; it is recorded by :func:`..wrapup.post_wrap_up`, and stays the
    summary for the rest of that session even as the board keeps changing. Sent
    once per board message: an edit keeps the ts, so it stays the summary.

    .. _code channel properties: https://docs.slack.dev/ai/slack-code/code-channel-properties#summary-message
    """
    from kiro_crew.slack.code_channels import wrapup

    # The wrap-up's own per-channel lock, held across the read, the choice, the
    # write and the pointer: a wrap-up recorded meanwhile is seen here and never
    # displaced (its own summary write follows its lock, so it lands last).
    async with wrapup._lock(binding.channel_id):
        await _record_summary_locked(binding, board_ts, admits)


async def _record_summary_locked(
    binding: Binding, board_ts: str, admits: Callable[[], bool] | None
) -> None:
    channel = binding.channel_id
    record = await get_async(channel)
    if record is not None and record.summary_message_ts == board_ts:
        return
    if (
        record is not None
        and record.summary_ts
        and record.summary_session_key == binding.session_key
    ):
        return
    client = surface.client()
    if client is None:
        return
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    # A setProperties write: the package's write gate (policy, archive, Slack's
    # Stop) and the board's scope are asked right before it, no await between.
    if await surface.write_refusal(channel, scope=lambda: _still(admits)):
        return
    try:
        await client.set_summary_message(channel, board_ts)
    except CodeChannelApiError as exc:
        logger.info("code channel %s: live summary not recorded: %s", channel, exc.code)
        from kiro_crew.slack.code_channels.notices import NOTIFIED, notify_refusal

        if exc.code in NOTIFIED:
            # Slack will refuse it again: not asked once more for this message.
            await notify_refusal(channel, exc.code, session_key=binding.session_key)
            await update_async(channel, summary_message_ts=board_ts)
        return
    try:
        await update_async(channel, summary_message_ts=board_ts)
    except Exception:
        logger.debug("code channel %s: summary pointer not stored", channel, exc_info=True)


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
    admits: Callable[[], bool] | None = None,
) -> None:
    """Thread one owner mention per worker report that waits on a person."""
    waiting = [row for row in snapshot.rows if row.needs_owner]
    record = await get_async(channel)
    unsaved = _unsaved_alerts.get(channel)
    sent = list(unsaved) if unsaved is not None else list(record.board_alerts) if record else []
    # An approval alert lives only while that approval is pending: once it is
    # answered it is forgotten, so the NEXT approval pages again.
    live = {alert_token(row) for row in waiting}
    kept = [t for t in sent if not _is_approval_alert(t) or t in live]
    cleared = len(kept) != len(sent)
    sent = kept
    fresh = [row for row in waiting if alert_token(row) not in sent]
    for row in fresh:
        if not _still(admits):
            break  # the channel stopped admitting the board: no more mentions
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
            _unsaved_alerts.pop(channel, None)
        except Exception:
            # The mentions were sent: the next publish must not send them again.
            if len(_unsaved_alerts) > _MAX_TRACKED:
                _unsaved_alerts.clear()
            _unsaved_alerts[channel] = sent[-MAX_ALERTS:]
            logger.debug("code channel %s: alert tokens not stored", channel, exc_info=True)

"""Agent-mode execution: the autonudge-driven ``kirocrew-research`` worker.

Owns the worker's run: preparing a launch (consuming the previous run's stop
evidence before the campaign is published RUNNING), arming the autonudge loop
on the app-owned worker slot with the brief, its model pin, title and bounded
trust, pausing or tearing that loop down, the worker's explicit end-of-run
``worker_done.json`` marker, and the cycle accounting the watchdog runs when a
new finding lands. This module binds, once, what the watchdog and the
workflow adapter share with it: the autonudge service accessor, the stop
reason, the worker-slot identity (``research-<cid>`` also names a workflow run)
and the link screens for the agent-writable campaign directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import stat
import time
from pathlib import Path

from aiohttp import web

from kiro_crew.apps.builtins.auto_research.campaign import (
    LOGGER_NAME,
    exploration,
    lifecycle,
    publication,
    storage,
    untrusted,
)
from kiro_crew.apps.builtins.auto_research.session_keys import (
    AUTO_RESEARCH_APP,
    research_slot_key,
)
from kiro_crew.autonudge import AUTONUDGE_STOP_REASON
from kiro_crew.autonudge import get_instance as _autonudge_instance
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.platform_compat import is_link_or_junction, unlink_link_or_junction

logger = logging.getLogger(LOGGER_NAME)

# Per-cycle trigger injected by the autonudge loop. The full methodology lives in
# the kirocrew-research agent's system prompt, so this only needs to name the cycle.
_RESEARCH_AGENT = "kirocrew-research"
_RESEARCH_NUDGE = (
    "Run the next research cycle for campaign {cid} "
    "(dir {dir}). Follow your per-cycle research "
    "protocol and end the turn when done."
)


_WORKER_DONE_FILENAME = "worker_done.json"
# The marker is LLM-written: bound how much of it the gateway will ever read.
# A legitimate marker is one short JSON object, so 64 KiB is already generous.
_WORKER_DONE_MAX_BYTES = 64 * 1024


def _read_worker_done(campaign_id: str) -> dict | None:
    """Read the worker's explicit end-of-run marker, or None.

    The worker writes ``worker_done.json`` in its campaign dir immediately
    before ending its run via ``autonudge_stop`` (instructed in the brief).
    This LLM-written marker is the compatibility fallback when the source-owned
    ``autonudge_stop`` tombstone is unavailable. Unlike the mere absence of the
    autonudge loop — which also happens when a deleted/closed worker session
    makes the nudge fire path retire the loop (``_fire_dashboard_nudge``:
    session unreachable → ``remove()``) — the marker file can only exist
    because the worker chose to finish. Malformed content, a non-object
    payload, or a missing/non-string/empty ``reason`` is treated as absent
    (fail toward FAILED, the conservative verdict) — the brief instructs the
    worker to write ``{"reason": "<one line>"}``, so anything else is not a
    deliberate completion signal.

    The path is LLM-writable, so the read itself is guarded: links (POSIX
    symlink or Windows junction) and non-regular files are rejected outright —
    a marker symlinked to ``/dev/zero`` must not become an unbounded read on
    the gateway — and at most ``_WORKER_DONE_MAX_BYTES`` are ever read; an
    over-cap file is treated as absent, never truncated-and-parsed.
    """
    d = storage._safe_campaign_dir(campaign_id)
    if d is None:
        return None
    marker = d / _WORKER_DONE_FILENAME
    try:
        if is_link_or_junction(marker):
            return None
        st = marker.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _WORKER_DONE_MAX_BYTES:
            return None
        # Cap at open time too (the file can grow between stat and read):
        # read one byte past the cap so an over-cap file is detected and
        # rejected rather than silently truncated into valid-looking JSON.
        with open(marker, "rb") as fh:
            raw = fh.read(_WORKER_DONE_MAX_BYTES + 1)
        if len(raw) > _WORKER_DONE_MAX_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return data


def _clear_worker_done_marker(campaign_id: str) -> None:
    """Remove a stale ``worker_done.json`` so a fresh run cannot inherit it.

    The campaign dir is LLM-writable, so tolerate a rogue DIRECTORY at the
    marker path too: ``unlink()`` would raise ``IsADirectoryError`` mid-resume
    (status already RUNNING, worker never launched, HTTP 500). ``rmtree`` only
    for a REAL directory — any link (POSIX symlink or Windows junction, per
    ``platform_compat.is_link_or_junction``; a junction reports ``is_dir()``
    True and ``is_symlink()`` False) is removed as a link so a link into a
    foreign tree can never recursively delete its target's contents.
    """
    d = storage._safe_campaign_dir(campaign_id)
    if d is None:
        return
    marker = d / _WORKER_DONE_FILENAME
    if is_link_or_junction(marker):
        unlink_link_or_junction(marker)
    elif marker.is_dir():
        shutil.rmtree(marker, ignore_errors=True)
    else:
        marker.unlink(missing_ok=True)


def _persist_new_cycle_bookkeeping(campaign_id: str, cycle_files: list[Path]) -> dict:
    """Persist one observed cycle advance and run its recursive-exploration step."""
    count = len(cycle_files)
    latest = storage._read_finding_file(cycle_files[-1])
    db = storage._get_db()
    try:
        db.execute("BEGIN")
        db.execute(
            "UPDATE campaigns SET total_cycles=? WHERE id=?",
            (count, campaign_id),
        )
        db.commit()
    finally:
        db.close()
    # File and SQLite work in recursive exploration belongs on the same worker
    # thread as the finding read and cycle-count persistence.
    exploration._advance_exploration(campaign_id)
    return latest


async def _record_new_cycle_from_watchdog(
    campaign_id: str,
    cycle_files: list[Path],
    last_counts: dict[str, int],
    last_ts: dict[str, float],
) -> dict:
    """Record a newly observed cycle without blocking the gateway event loop.

    The SSE fires from the worker thread right after the bookkeeping persists
    (same cancellation contract as ``_guarded_transition``'s ``on_commit``).
    """
    event_loop = asyncio.get_running_loop()

    def _persist_and_notify() -> dict:
        latest = _persist_new_cycle_bookkeeping(campaign_id, cycle_files)
        lifecycle._sse_from_thread(
            event_loop,
            {"type": "new_finding", "campaign_id": campaign_id, "finding": latest},
        )
        return latest

    latest = await asyncio.to_thread(_persist_and_notify)
    last_counts[campaign_id] = len(cycle_files)
    last_ts[campaign_id] = time.time()
    return latest


async def _prepare_loop_launch(cid: str) -> None:
    """Remove prior-run stop evidence before a campaign becomes RUNNING.

    The watchdog queries RUNNING campaigns, so callers must await this helper
    before publishing that state. Otherwise a slow marker cleanup can expose a
    resumed campaign alongside its previous run's tombstone, letting the
    watchdog settle the new run before its worker is armed.
    """
    # A fresh run must not inherit the previous run's deliberate-stop signals:
    # stale marker/tombstone evidence would classify a genuine stall of THIS
    # run as STOPPED. Consume the source-owned tombstone before the potentially
    # slow marker cleanup so even direct _launch_loop callers preserve that
    # ordering. The marker path is LLM-writable, so its cleanup runs off-loop
    # and may rmtree an arbitrarily large rogue directory.
    svc = _autonudge_instance()
    if svc is not None:
        previous = svc.get_by_slot(research_slot_key(cid))
        if (
            previous is not None
            and not previous.active
            and str(getattr(previous, "stopped_reason", "") or "") == AUTONUDGE_STOP_REASON
        ):
            await svc.remove(previous.id)
    await asyncio.to_thread(_clear_worker_done_marker, cid)


async def _launch_loop(request: web.Request, cid: str, *, prepared: bool = False) -> None:
    """Arm an autonudge loop that drives the research cycles for this campaign.

    Best-effort: if autonudge or dashboard state is unavailable, the status
    change still stands but no worker is launched (logged for visibility).
    """
    if not prepared:
        await _prepare_loop_launch(cid)
    state = request.app.get("state")
    svc = _autonudge_instance()
    if state is None or svc is None:
        logger.warning(
            "auto_research: cannot launch loop for %s (autonudge/state unavailable)", cid
        )
        return

    def _read_launch_row_and_write_brief() -> sqlite3.Row | None:
        """Row read + brief render in ONE write transaction.

        ``BEGIN IMMEDIATE`` serializes this against ``_append_question``'s
        transaction: a concurrent Add Question either commits before (this
        brief includes it) or waits until after (its own in-transaction brief
        write lands last, from the fresher row). Two separate hops here would
        let a stale snapshot overwrite a just-committed question's brief.
        """
        with publication._brief_publish_lock(cid):
            db = storage._get_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT name, question, sub_questions, sources, scope_constraints, max_cycles, idle_secs, "
                    "success_criteria, auto_approve, parallel_workers, model FROM campaigns WHERE id = ?",
                    (cid,),
                ).fetchone()
                if row is None:
                    db.execute("ROLLBACK")
                    return None
                db.commit()
            finally:
                db.close()
            # Publish AFTER commit (a rollback must never leave a brief that
            # describes phantom state); the publish lock spans commit+write so
            # publish order matches commit order.
            publication._write_brief(cid, row)
            return row

    row = await asyncio.to_thread(_read_launch_row_and_write_brief)
    if row is None:
        return
    # Pin the campaign's explicit model pick on the worker slot ('' = inherit
    # the research agent's / backend's default resolution — never a hardcoded
    # id here). If a concrete pick is not served for this account, the session
    # layer's withhold (_pinned_model_verdict) KEEPS the pin and runs the
    # worker on the backend default — the notice it posts lands in the hidden
    # research-<cid> transcript, not on the Research Lab page.
    campaign_model = row["model"] or ""
    slot_key = research_slot_key(cid)
    slot = state.get_or_create_slot(
        name=slot_key,
        agent=_RESEARCH_AGENT,
        app=AUTO_RESEARCH_APP,
        model=campaign_model,
    )
    # get_or_create_slot only applies kwargs on CREATE; on resume the slot
    # already exists, so re-pin explicitly — the campaign row stays the single
    # source of truth for the worker's model across gateway restarts.
    slot.model = campaign_model
    # Give the app-owned worker slot a meaningful title (the campaign's human
    # name) instead of the "New Session…" placeholder. The slot is driven by
    # autonudge, whose injected messages carry role "nudge" (not "user"), so the
    # normal LLM auto-titler never fires for it (_maybe_auto_title gates on
    # user_count >= 1). Set it explicitly, mirroring the cron/workflow slot
    # pattern: redact user-supplied text (defence-in-depth), lock _titled so
    # display_title returns it instead of the placeholder, persist so it survives
    # a gateway restart, and push a live SSE update to the sidebar/header.
    raw_title = row["name"] or slot_key
    if untrusted._HAS_SECURITY:
        raw_title, _ = untrusted.redact_exfiltration_urls(raw_title)
        raw_title, _ = untrusted.redact_credentials(raw_title)
    else:
        # Fail closed: the campaign name is user-controlled, so if the security
        # redactors are unavailable we must NOT persist/broadcast it. Fall back
        # to the non-user-derived slot key, which carries no user content.
        raw_title = slot_key
    slot.title = raw_title
    slot._titled = True
    # Persist the title so it survives a gateway restart. set_title() does
    # synchronous file I/O (read + rewrite + fsync), so offload it to a thread
    # to avoid blocking the event loop, and treat persistence as best-effort:
    # a slow/failed write must never prevent the worker loop from being armed
    # below (otherwise the campaign would be left running with no worker).
    if getattr(state, "conversation_log", None) is not None:
        try:
            await asyncio.to_thread(
                state.conversation_log.set_title, slot_history_key(slot), slot.title
            )
        except Exception:
            logger.warning("auto_research: failed to persist slot title for %s", cid, exc_info=True)
    state.push_slot_title(slot.key, slot.title)
    # The worker runs autonomously — auto-approve its tools so the loop never
    # stalls on per-tool approval prompts (brakes: max_cycles, Stop, sandbox,
    # deny-list). The slot is app-owned, so it's hidden from the chat sidebar.
    # NOTE: slot._trust is the PER-SLOT trust flag (same mechanism as the
    # interactive "trust this session" in chat_handlers.py and gateway scoped
    # trust) — NOT the global _yolo_mode that safety_override() governs, which is
    # a single process-wide toggle and cannot express per-campaign grants. The
    # grant is instead bounded per campaign: the watchdog expires it after
    # _TRUST_TTL_SECS and forces NEEDS_INPUT re-authorization (see _watchdog_loop).
    slot._trust = True
    lifecycle._audit("campaign_auto_approve", cid)
    state.push_slots_update()  # surface the app-owned worker slot so the UI filters it
    await svc.add(
        slot_key=slot.key,
        message=_RESEARCH_NUDGE.format(cid=cid, dir=storage._campaign_dir(cid)),
        idle_secs=int(row["idle_secs"] or lifecycle.DEFAULT_IDLE_SECS),
        max_cycles=int(row["max_cycles"] or 0),
        stop_sentinel_path=str(storage._campaign_dir(cid) / "STOP"),
        admission_check=lambda: state.get_slot(slot.key) is slot,
    )


async def _stop_loop(cid: str, *, remove: bool, stop_reason: str = "") -> None:
    """Pause (remove=False) or tear down (remove=True) a campaign's autonudge loop.

    ``stop_reason`` names a teardown in the loop's stop log line.
    """
    svc = _autonudge_instance()
    if svc is None:
        return
    loop = svc.get_by_slot(research_slot_key(cid))
    if not loop:
        return
    if remove:
        await svc.remove(loop.id, stop_reason=stop_reason)
    else:
        await svc.update(loop.id, active=False)

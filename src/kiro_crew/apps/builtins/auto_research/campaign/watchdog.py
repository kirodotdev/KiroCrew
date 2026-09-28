"""The Research Lab watchdog: the per-cycle policy over every RUNNING campaign.

Each poll gates on the app's live enabled flag (suspending research loops and
their slot trust while disabled), hands workflow-mode campaigns to their
adapter, and for agent-mode campaigns: expires the 24h auto-approve trust,
re-arms trust and the loop, pauses an attended campaign on a pending question,
records new cycles, and settles a run as COMPLETE, STAGNANT, STOPPED or FAILED.
Settlement is fenced to the run generation it observed and persists before the
worker's loop is removed, so a stale poll cannot settle a newer run and a
shutdown cannot leave a terminal campaign with a re-armable loop. The task that
runs the loop is owned by ``handlers.register_routes``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.auto_research.campaign import (
    LOGGER_NAME,
    agent_mode,
    lifecycle,
    storage,
    workflow_mode,
)
from kiro_crew.apps.builtins.auto_research.campaign.storage import CampaignStatus
from kiro_crew.apps.builtins.auto_research.session_keys import is_research_slot_key
from kiro_crew.apps.manager import is_app_enabled

logger = logging.getLogger(LOGGER_NAME)

POLL_INTERVAL = 5
_TERMINAL_LOOP_REMOVAL_ATTEMPTS = 3
# The first cycle's startup grace: it can't produce anything until the first
# nudge + a full work turn.
_FIRST_CYCLE_GRACE_SECS = 600
# Worker auto-approve is capped at 24h; past this the watchdog pauses the
# campaign to NEEDS_INPUT and it must be resumed (re-authorized) to continue.
_TRUST_TTL_SECS = 24 * 3600


def _unresponsive_deadline(idle_secs: int) -> int:
    """Idle seconds (no slot activity AND no new finding) before unresponsive.

    Generous floor: a deep research cycle can take minutes (web fetches +
    synthesis), so a tight idle_secs*2 window falsely fails healthy slow cycles.
    The watchdog also resets this timer whenever the worker slot is actively
    running a turn, so this only bounds genuine no-activity stalls.
    """
    return max(idle_secs * 2, _FIRST_CYCLE_GRACE_SECS)


def check_stagnation(campaign_id: str) -> bool:
    d = storage._safe_campaign_dir(campaign_id)
    if not d:
        return False
    findings_dir = d / "findings"
    if not findings_dir.exists():
        return False
    files = storage._cycle_finding_files(findings_dir)
    if len(files) < 5:
        return False
    for f in files[-5:]:
        try:
            # LLM-written cycle file: pin UTF-8 (Windows would otherwise decode
            # with the ANSI code page) and absorb bad bytes, because a decode
            # error here would abort the whole watchdog sweep.
            raw = f.read_text(encoding="utf-8", errors="replace")
            if json.loads(raw).get("new_findings_count", 0) > 0:
                return False
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return False
    return True


async def _expire_trust(cid: str, observed_started_at: float | None) -> None:
    """24h auto-approve expiry: park the campaign for re-authorization.

    Transition FIRST, then write the synthetic question only if it persisted:
    a refused transition (a user Stop committed during the hop) must not leave
    a stale question file behind — it would drag a later Resume straight back
    into NEEDS_INPUT with an expiry prompt that does not apply.
    ``observed_started_at`` fences the write to the run generation whose age
    was actually measured — a Pause→Resume replacement run must not be parked
    by the previous run's expiry verdict.
    """
    event_loop = asyncio.get_running_loop()

    def _on_parked(_result: dict) -> None:
        # Runs in the txn thread right after the transition persists — survives
        # a cancellation of the awaiting watchdog frame (see _guarded_transition).
        qpath = storage._questions_path(cid)
        if qpath:
            try:
                # The path lives in the agent-writable research dir: clear a
                # link/junction or directory squatting on it before writing, and
                # never let a write failure suppress the audit/SSE for a
                # transition that already persisted.
                if agent_mode.is_link_or_junction(qpath):
                    agent_mode.unlink_link_or_junction(qpath)
                elif qpath.is_dir():
                    shutil.rmtree(qpath)
                qpath.write_text(
                    json.dumps(
                        {
                            "question": "Auto-approval expired after 24h. Resume to "
                            "re-authorize and continue."
                        }
                    ),
                    encoding="utf-8",
                )
            except OSError:
                logger.warning(
                    "auto_research: could not publish the expiry prompt for %s "
                    "(campaign is parked NEEDS_INPUT; Resume still works)",
                    cid,
                    exc_info=True,
                )
        lifecycle._audit("campaign_trust_expired", cid)
        lifecycle._sse_from_thread(event_loop, {"type": "needs_input", "campaign_id": cid})

    await lifecycle._guarded_transition(
        cid,
        CampaignStatus.NEEDS_INPUT,
        allowed_current=(CampaignStatus.RUNNING,),
        expected_started_at=observed_started_at,
        on_commit=_on_parked,
    )


def _should_pause_for_question(cid: str, auto_approve: bool) -> bool:
    """Decide what to do with a pending questions.json.

    Returns True only when the campaign should pause to NEEDS_INPUT (attended
    mode with a question waiting). Unattended mode NEVER pauses: any stray
    question (the agent was not given a questions directive) is discarded so
    "unattended" is a code-enforced guarantee, not reliant on the LLM obeying
    a prompt. Returns False when there's no question or it was discarded.
    """
    qp = storage._questions_path(cid)
    if not (qp and qp.exists()):
        return False
    if auto_approve:
        qp.unlink(missing_ok=True)
        lifecycle._audit("campaign_unattended_question_discarded", cid)
        return False
    return True


async def _suspend_research_loops_while_disabled(state: Any) -> None:
    """Deactivate every research autonudge loop and clear its slot trust.

    Called from the watchdog when the app is disabled. The 24h trust expiry lives
    in the per-campaign body that a disabled cycle skips, and autonudge loops fire
    regardless of the enabled flag — so without this a disabled app keeps a running
    campaign's tools auto-approved indefinitely past the cap. Idempotent: once the
    loops are inactive and trust is cleared, later disabled cycles are no-ops.
    Re-enabling restores trust and re-arms the loop in the per-campaign body.
    """
    svc = agent_mode._autonudge_instance()
    if svc is None:
        return
    for loop in svc.list_all():
        if not is_research_slot_key(loop.slot_key):
            continue
        if loop.active:
            try:
                await svc.update(loop.id, active=False)
            except Exception:  # noqa: BLE001 — disable cleanup must not raise
                logger.warning("auto_research: could not deactivate loop %s on disable", loop.id)
        slot = state._slots.get(loop.slot_key) if state is not None else None
        if slot is not None and getattr(slot, "_trust", False):
            slot._trust = False


def _stalled_campaign_verdict(
    campaign_id: str,
    cycle_files: list[Path],
    *,
    stopped_reason: str = "",
) -> tuple[CampaignStatus, str | None]:
    """Classify an idle-deadline expiry — not every silence is a failure.

    The watchdog only marks COMPLETE when a NEW cycle file arrives carrying
    ``verification.passed=true`` (or the cycle cap is hit). A worker that ends
    its run deliberately via ``autonudge_stop`` — goal met, nothing more to
    write — produces no further findings, so silence up to the unresponsive
    deadline is not evidence of a stall: stamping FAILED ("research stalled")
    would contradict a finished report on disk. Distinguish the cases from durable
    evidence:

    - Latest finding has ``verification.passed=true`` → COMPLETE. Also heals a
      completed campaign whose status was later reset to RUNNING (resume paths
      allow terminal→RUNNING): with no new files the count never advances, so
      the count>prev COMPLETE branch can never re-fire.
    - A source-owned ``autonudge_stop`` tombstone, or as a fallback the
      worker-written ``worker_done.json`` marker, plus a READABLE latest
      finding → the worker ended the run on purpose → STOPPED. The tombstone
      wins without reading the LLM-written marker. Same terminal affordances
      as a user Stop (fork / export / add-to-knowledge), no red failure banner.
      A stop signal alongside only unreadable findings is NOT a deliberate
      finish — STOPPED's "findings are preserved" promise would be false — so
      it falls through to FAILED. Mere ABSENCE of the autonudge loop is
      deliberately NOT used as the signal: the nudge fire path also removes
      loops for unreachable (deleted/closed) worker sessions, which is a
      failure, not a finish.
    - Otherwise → FAILED (genuine stall), unchanged.
    """
    if cycle_files:
        latest = storage._read_finding_file(cycle_files[-1])
        verified = latest.get("verification")
        if isinstance(verified, dict) and verified.get("passed") is True:
            return CampaignStatus.COMPLETE, None
        deliberate_stop = stopped_reason == agent_mode.AUTONUDGE_STOP_REASON
        if latest and (deliberate_stop or agent_mode._read_worker_done(campaign_id) is not None):
            return (
                CampaignStatus.STOPPED,
                "Worker ended the research loop — findings are preserved.",
            )
    return (
        CampaignStatus.FAILED,
        "No activity — research stalled. Resume to continue.",
    )


async def _settle_campaign_from_watchdog(
    campaign_id: str,
    cycle_files: list[Path],
    last_counts: dict[str, int],
    last_ts: dict[str, float],
    *,
    observed_started_at: float | None,
    stopped_reason: str = "",
) -> None:
    """Classify one terminal signal and cancellation-safely remove its loop."""

    # Bind cleanup to the loop that produced this terminal observation. Status
    # persistence makes Resume legal and may be slow; Resume can replace the
    # slot-bound loop before settlement continues. Re-resolving by slot after
    # that await would delete the replacement and leave RUNNING with no worker.
    svc = agent_mode._autonudge_instance()
    terminating_loop = svc.get_by_slot(agent_mode.research_slot_key(campaign_id)) if svc else None
    terminating_loop_id = terminating_loop.id if terminating_loop is not None else None

    async def _settle() -> None:
        async def _remove_terminating_loop() -> None:
            try:
                if svc is not None and terminating_loop_id is not None:
                    for attempt in range(1, _TERMINAL_LOOP_REMOVAL_ATTEMPTS + 1):
                        try:
                            await svc.remove(terminating_loop_id)
                        except OSError:
                            if attempt == _TERMINAL_LOOP_REMOVAL_ATTEMPTS:
                                raise
                            logger.warning(
                                "Auto Research: retrying durable loop removal for %s "
                                "after store failure (%s/%s)",
                                campaign_id,
                                attempt,
                                _TERMINAL_LOOP_REMOVAL_ATTEMPTS,
                            )
                        else:
                            break
            finally:
                last_counts.pop(campaign_id, None)
                last_ts.pop(campaign_id, None)

        async with lifecycle._campaign_transition_lock(campaign_id):
            if not await asyncio.to_thread(
                lifecycle._campaign_run_is_current,
                campaign_id,
                observed_started_at,
            ):
                return
            if len(cycle_files) > last_counts.get(campaign_id, 0):
                # The worker may publish its final finding and stop tombstone in the
                # same turn. Preserve the ordinary cycle bookkeeping before the
                # terminal fast path consumes the loop record.
                await agent_mode._record_new_cycle_from_watchdog(
                    campaign_id,
                    cycle_files,
                    last_counts,
                    last_ts,
                )
            status, message = await asyncio.to_thread(
                _stalled_campaign_verdict,
                campaign_id,
                cycle_files,
                stopped_reason=stopped_reason,
            )
            # Persist a non-rearmable loop state before SQLite becomes terminal.
            # If the later removal write fails, restart may retain this exact
            # loop, but it cannot schedule another worker turn.
            if svc is not None and terminating_loop is not None and terminating_loop.active:
                await svc.update(
                    terminating_loop.id,
                    active=False,
                    stopped_reason=f"campaign_{status.value}",
                )
            try:
                await asyncio.to_thread(
                    lifecycle.update_campaign_status,
                    campaign_id,
                    status,
                    error_message=message,
                )
            except Exception:
                # SQLite commits before the status sidecar and audit write. If
                # either later step fails, the campaign is already terminal and
                # retaining its persisted loop would re-arm it after restart.
                # Bind the recovery to this observed generation and verdict so a
                # failure before the commit still keeps the non-terminal loop for
                # a later retry.
                terminal_committed = await asyncio.to_thread(
                    lifecycle._campaign_run_has_status,
                    campaign_id,
                    observed_started_at,
                    status,
                )
                if terminal_committed:
                    await _remove_terminating_loop()
                raise
            await _remove_terminating_loop()
            lifecycle._emit_sse({"type": status.value, "campaign_id": campaign_id})

    def _report_terminal_settlement(settled: "asyncio.Task[Any]") -> None:
        # Retrieve and report the worker failure without letting it replace the
        # watchdog's shutdown cancellation.
        try:
            settled.result()
        except asyncio.CancelledError:
            logger.error("auto_research terminal settlement was cancelled")
        except Exception:
            # Preserve shutdown cancellation even when persistence fails. The
            # campaign remains non-terminal and its active loop can retry after
            # restart instead of leaving shutdown stuck in the watchdog loop.
            logger.exception("auto_research terminal settlement failed during shutdown")

    # Status persistence and loop removal are one terminal transition. A
    # shutdown cancellation after SQLite commits must not leave an active
    # persisted loop that start() can re-arm for a terminal campaign, so settle
    # the cleanup task before the cancellation propagates.
    settlement = asyncio.create_task(_settle())
    await lifecycle._settle_before_cancellation(settlement, on_settled=_report_terminal_settlement)


async def _watchdog_loop(app: web.Application | None = None) -> None:
    event_loop = asyncio.get_running_loop()  # for _sse_from_thread in on_commit hooks
    state = app.get("state") if app is not None else None
    last_counts: dict[str, int] = {}
    last_ts: dict[str, float] = {}
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
            # A builtin whose background loop must respect enabled state:
            # register_routes always appends this loop at startup, so gate every
            # cycle on the app's live enabled flag before doing any DB work.
            # Checking per-cycle (not once at startup) means enabling the app
            # later starts work without a gateway restart, and disabling it stops
            # the work. is_app_enabled reads installed.json synchronously, so run
            # it off the event loop.
            if not await asyncio.to_thread(is_app_enabled, agent_mode.AUTO_RESEARCH_APP):
                # Disabling the app must NOT leave a running campaign auto-approved.
                # The per-campaign 24h trust expiry lives in the body below, which a
                # disabled cycle skips, and the autonudge loops fire regardless of the
                # enabled flag — so without this a disabled app keeps a slot's
                # _trust=True and its loop nudging past the 24h cap. Deactivate every
                # research loop and clear its slot trust first; re-enabling
                # re-establishes trust and re-arms the loop in the per-campaign body.
                await _suspend_research_loops_while_disabled(state)
                continue

            def _read_active_campaigns() -> list[sqlite3.Row]:
                db = storage._get_db()
                try:
                    return db.execute(
                        "SELECT id, idle_secs, max_cycles, started_at, auto_approve, execution_mode "
                        "FROM campaigns WHERE status = ?",
                        (CampaignStatus.RUNNING,),
                    ).fetchall()
                finally:
                    db.close()

            active = await asyncio.to_thread(_read_active_campaigns)
            for row in active:
                cid = row["id"]
                # Workflow-mode campaigns are driven by a Dynamic Workflow run;
                # the adapter translates its events/result into the RL file+SSE
                # model. The agent-mode body below does not apply to them.
                if row["execution_mode"] == "workflow":
                    await workflow_mode._poll_workflow_campaign(cid, state, row["started_at"])
                    continue
                slot_key = agent_mode.research_slot_key(cid)
                slot = state._slots.get(slot_key) if state is not None else None
                svc = agent_mode._autonudge_instance()
                loop = svc.get_by_slot(slot_key) if svc is not None else None
                started = row["started_at"]
                run_newly_observed = cid not in last_counts or last_ts.get(cid, 0.0) < (
                    started or 0
                )
                if loop is not None and not loop.active:
                    stopped_reason = str(getattr(loop, "stopped_reason", "") or "")
                    if stopped_reason == agent_mode.AUTONUDGE_STOP_REASON:
                        if run_newly_observed:
                            # A resume marks the campaign RUNNING before _launch_loop
                            # removes the previous run's tombstone. Establish this
                            # run's observation boundary before trusting stop evidence
                            # so a watchdog poll in that window cannot settle the new
                            # run. Keep the tombstone inactive while launch catches up.
                            cycle_files = await asyncio.to_thread(storage._list_cycle_files, cid)
                            last_counts[cid] = len(cycle_files)
                            last_ts[cid] = time.time()
                        # The directive runs inside the worker turn. Removing
                        # its loop before that turn exits would cancel the
                        # firing timer and destroy the response/bookkeeping.
                        if slot is not None and slot.running:
                            continue
                        if run_newly_observed:
                            continue
                        cycle_files = await asyncio.to_thread(storage._list_cycle_files, cid)
                        await _settle_campaign_from_watchdog(
                            cid,
                            cycle_files,
                            last_counts,
                            last_ts,
                            observed_started_at=started,
                            stopped_reason=stopped_reason,
                        )
                        continue
                # 24h auto-approve cap: expire trust and require re-authorization.
                if started and time.time() - started > _TRUST_TTL_SECS:
                    if slot is not None:
                        slot._trust = False
                    await _expire_trust(cid, started)
                    continue
                # Re-establish worker trust each cycle (restart-durable; bounded above).
                if slot is not None and not slot._trust:
                    slot._trust = True
                    lifecycle._audit("campaign_trust_reestablished", cid)
                # Re-arm the autonudge loop if a prior app-disable deactivated it
                # (see _suspend_research_loops_while_disabled at the enabled guard).
                if svc is not None and loop is not None and not loop.active:
                    await svc.update(loop.id, active=True)
                # Attended: pause for the user. Unattended: discard the stray
                # question + keep running (code-enforced; see helper).
                if _should_pause_for_question(cid, bool(row["auto_approve"])):
                    await lifecycle._guarded_transition(
                        cid,
                        CampaignStatus.NEEDS_INPUT,
                        allowed_current=(CampaignStatus.RUNNING,),
                        expected_started_at=started,
                        on_commit=lambda _r, cid=cid: lifecycle._sse_from_thread(
                            event_loop, {"type": "needs_input", "campaign_id": cid}
                        ),
                    )
                    continue
                # Lightweight: count files without reading them all. Only parse
                # the latest finding when count advances (avoids re-reading 50+
                # JSON files every 5s).
                cycle_files = storage._list_cycle_files(cid)
                count = len(cycle_files)
                if run_newly_observed:
                    last_counts[cid] = count
                    last_ts[cid] = time.time()
                    continue
                prev = last_counts[cid]
                if count > prev:
                    latest = await agent_mode._record_new_cycle_from_watchdog(
                        cid,
                        cycle_files,
                        last_counts,
                        last_ts,
                    )
                    verified = latest.get("verification")
                    if isinstance(verified, dict) and verified.get("passed") is True:
                        await lifecycle._guarded_transition(
                            cid,
                            CampaignStatus.COMPLETE,
                            allowed_current=(CampaignStatus.RUNNING,),
                            expected_started_at=started,
                            on_commit=lambda _r, cid=cid: lifecycle._sse_from_thread(
                                event_loop, {"type": "complete", "campaign_id": cid}
                            ),
                        )
                    elif count >= row["max_cycles"]:
                        await lifecycle._guarded_transition(
                            cid,
                            CampaignStatus.COMPLETE,
                            allowed_current=(CampaignStatus.RUNNING,),
                            expected_started_at=started,
                            on_commit=lambda _r, cid=cid: lifecycle._sse_from_thread(
                                event_loop, {"type": "complete", "campaign_id": cid}
                            ),
                        )
                    elif check_stagnation(cid):
                        await lifecycle._guarded_transition(
                            cid,
                            CampaignStatus.STAGNANT,
                            allowed_current=(CampaignStatus.RUNNING,),
                            expected_started_at=started,
                            on_commit=lambda _r, cid=cid: lifecycle._sse_from_thread(
                                event_loop, {"type": "stagnant", "campaign_id": cid}
                            ),
                        )
                elif cid in last_ts:
                    if slot is not None and slot.running:
                        # Agent is actively working this cycle (deep research can
                        # take minutes) — alive, not unresponsive. Refresh liveness.
                        last_ts[cid] = time.time()
                    elif time.time() - last_ts[cid] > _unresponsive_deadline(row["idle_secs"]):
                        # Deadline expired — but classify before condemning: a
                        # worker that deliberately ended its run (worker_done
                        # marker; verified finding on disk) finished, it didn't
                        # stall. See _stalled_campaign_verdict. Off the event
                        # loop: it reads LLM-written files (finding + marker)
                        # whose size is unbounded, and this watchdog shares the
                        # gateway's single loop with every request and the
                        # heartbeat (no-blocking-call-on-event-loop).
                        await _settle_campaign_from_watchdog(
                            cid,
                            cycle_files,
                            last_counts,
                            last_ts,
                            observed_started_at=started,
                        )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("auto_research watchdog error")
            await asyncio.sleep(POLL_INTERVAL)

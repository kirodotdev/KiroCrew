"""Workflow-mode execution: a campaign run as a Dynamic Workflow.

Owns starting the research template on the gateway's ``WorkflowService``,
cancelling it, and the watchdog adapter that translates a run's events and
result into the same cycle files, ``FINDINGS.md``, bookkeeping and SSE the
dashboard already consumes. ``workflow_run.json`` records the run id and the
cycle offset, so a run resumed after a pause appends findings rather than
re-indexing over the previous run's.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
    untrusted,
)
from kiro_crew.apps.builtins.auto_research.campaign.storage import CampaignStatus
from kiro_crew.apps.builtins.auto_research.workflow_template import (
    RESEARCH_WORKFLOW_SOURCE,
    build_workflow_args,
)

logger = logging.getLogger(LOGGER_NAME)

_WORKFLOW_RUN_FILE = "workflow_run.json"


def _write_workflow_run_id(campaign_id: str, run_id: str) -> None:
    d = storage._campaign_dir(campaign_id)
    # cycle_offset: number of cycle files already written by prior runs. Pause
    # cancels the DW run and resume launches a NEW run whose investigate events
    # restart at index 0; without this offset the adapter would re-index new
    # findings over the old ones (or drop them until the new run out-produced the
    # old). Persisting the offset makes the resumed run append correctly.
    cycle_offset = len(storage._list_cycle_files(campaign_id))
    d.joinpath(_WORKFLOW_RUN_FILE).write_text(
        json.dumps({"run_id": run_id, "ts": time.time(), "cycle_offset": cycle_offset}),
        encoding="utf-8",
    )


def _read_workflow_cycle_offset(campaign_id: str) -> int:
    d = storage._safe_campaign_dir(campaign_id)
    p = (d / _WORKFLOW_RUN_FILE) if d else None
    if not p or not p.exists():
        return 0
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("cycle_offset", 0) or 0)
    except (OSError, ValueError, TypeError):
        return 0


def _read_workflow_run_id(campaign_id: str) -> str | None:
    d = storage._safe_campaign_dir(campaign_id)
    p = (d / _WORKFLOW_RUN_FILE) if d else None
    if not p or not p.exists():
        return None
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("run_id") or "") or None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


async def _launch_workflow(request: web.Request, cid: str) -> None:
    """Start the research methodology as a Dynamic Workflow (workflow mode).

    Best-effort: if the gateway's WorkflowService is unavailable or the start
    fails, mark the campaign FAILED so it doesn't sit zombie in RUNNING. The
    watchdog adapter (`_poll_workflow_campaign`) translates the run's
    events/result into the same cycle/findings files + SSE the UI already
    consumes.
    """
    state = request.app.get("state")
    svc = getattr(state, "workflow_service", None) if state is not None else None
    if svc is None:
        logger.warning(
            "auto_research: workflow_service unavailable; cannot launch workflow for %s", cid
        )
        await asyncio.to_thread(
            lifecycle.update_campaign_status,
            cid,
            CampaignStatus.FAILED,
            error_message="Dynamic Workflow engine unavailable — cannot start workflow mode.",
        )
        lifecycle._emit_sse({"type": "failed", "campaign_id": cid})
        return

    def _read_workflow_row() -> sqlite3.Row | None:
        db = storage._get_db()
        try:
            return db.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
        finally:
            db.close()

    row = await asyncio.to_thread(_read_workflow_row)
    if row is None:
        return
    args = build_workflow_args(dict(row))
    try:
        res = await svc.start(
            RESEARCH_WORKFLOW_SOURCE, name=agent_mode.research_slot_key(cid), args=args
        )
    except Exception:
        logger.exception("auto_research: workflow start failed for %s", cid)
        await asyncio.to_thread(
            lifecycle.update_campaign_status,
            cid,
            CampaignStatus.FAILED,
            error_message="Workflow start failed — see gateway logs for details.",
        )
        lifecycle._emit_sse({"type": "failed", "campaign_id": cid})
        return
    run_id = (res or {}).get("run_id")
    if run_id:
        _write_workflow_run_id(cid, run_id)
        lifecycle._audit("campaign_workflow_started", cid)
    else:
        logger.warning("auto_research: workflow start returned no run_id for %s: %s", cid, res)
        await asyncio.to_thread(
            lifecycle.update_campaign_status,
            cid,
            CampaignStatus.FAILED,
            error_message="Workflow start returned no run ID.",
        )
        lifecycle._emit_sse({"type": "failed", "campaign_id": cid})


async def _stop_workflow(request: web.Request, cid: str) -> None:
    """Cancel a campaign's Dynamic Workflow run (workflow mode). Best-effort."""
    state = request.app.get("state")
    svc = getattr(state, "workflow_service", None) if state is not None else None
    run_id = _read_workflow_run_id(cid)
    if svc is not None and run_id:
        try:
            await svc.cancel(run_id)
        except Exception:
            logger.exception("auto_research: workflow cancel failed for %s", cid)


async def _poll_workflow_campaign(
    campaign_id: str, state: Any, observed_started_at: float | None
) -> None:
    """Adapter: translate a Dynamic Workflow run's events/result into the RL
    file + SSE model the existing UI consumes. Each `investigate:` agent that
    finishes becomes a cycle finding; on terminal the run's report is written to
    FINDINGS.md and the campaign is marked COMPLETE/FAILED. Best-effort — never
    raises into the watchdog. ``observed_started_at`` fences every terminal
    write to the run generation this poll actually observed.
    """
    try:
        event_loop = asyncio.get_running_loop()

        def _redact_llm(s: Any) -> str:
            text = str(s or "")
            if not untrusted._HAS_SECURITY:
                # Fail closed: strip the text entirely rather than persisting
                # potentially credential-laden LLM output to disk unredacted.
                return untrusted._redact_finding({"v": text})["v"] if text else ""
            cleaned, _ = untrusted.redact_credentials(text)
            cleaned, _ = untrusted.redact_exfiltration_urls(cleaned)
            return cleaned

        svc = getattr(state, "workflow_service", None) if state is not None else None
        run_id = _read_workflow_run_id(campaign_id)
        if svc is None or not run_id:
            return
        # svc.result() reads a file-backed snapshot (JSON on disk) — it does not
        # mutate the event-loop-affine registry. Offloading to a thread avoids
        # blocking the loop on file I/O while remaining safe to call concurrently
        # (reads only, no shared mutable state with the loop).
        snap = await asyncio.to_thread(svc.result, run_id)
        if not snap:
            # Bounded-poll fallback: if the run snapshot is gone (LRU eviction,
            # lost record) and the campaign has been RUNNING for > 1h with no
            # progress, mark it FAILED rather than let it sit zombie forever.
            d = storage._safe_campaign_dir(campaign_id)
            run_file = (d / _WORKFLOW_RUN_FILE) if d else None
            run_meta = (
                await asyncio.to_thread(storage._read_json_or_missing, run_file)
                if run_file
                else None
            )
            if isinstance(run_meta, dict):
                try:
                    started_ts = float(run_meta.get("ts", 0))
                    if started_ts and (time.time() - started_ts) > 3600:
                        await lifecycle._guarded_transition(
                            campaign_id,
                            CampaignStatus.FAILED,
                            allowed_current=(CampaignStatus.RUNNING,),
                            expected_started_at=observed_started_at,
                            on_commit=lambda _r: lifecycle._sse_from_thread(
                                event_loop,
                                {"type": "failed", "campaign_id": campaign_id},
                            ),
                            error_message="Workflow run snapshot lost after 1h — run likely evicted or crashed.",
                        )
                except (OSError, ValueError, TypeError):
                    pass
            return
        # ALL snapshot processing runs under the campaign's transition lock:
        # the slow snapshot read above happens outside it, so a user Pause →
        # Resume may have replaced the run generation while we were reading.
        # Re-verify the generation at lock entry and abort processing entirely
        # when stale — a stale poll must not write cycle files, bookkeeping, or
        # terminal state into the REPLACEMENT run. The lock also excludes
        # _handle_action mid-processing, so check-then-write below is atomic
        # with respect to user actions.
        async with lifecycle._campaign_transition_lock(campaign_id):
            if not await asyncio.to_thread(
                lifecycle._campaign_run_is_current, campaign_id, observed_started_at
            ):
                return  # replacement run took over while we read the snapshot
            d = storage._campaign_dir(campaign_id)
            events = snap.get("events") or []
            # Correlate agent_started (carries label/phase) -> agent_finished by id.
            started: dict = {}
            for e in events:
                if e.get("type") == "agent_started":
                    data = e.get("data") or {}
                    started[data.get("agent_id")] = data
            investigate: list = []
            for e in events:
                if e.get("type") == "agent_finished":
                    data = e.get("data") or {}
                    meta = started.get(data.get("agent_id"), {})
                    if str(meta.get("label", "")).startswith("investigate") and data.get("ok"):
                        investigate.append((meta, data))
            cycle_offset = _read_workflow_cycle_offset(campaign_id)
            wrote = False
            # Each investigation maps to one cycle file (intentional: the UI shows
            # per-investigation progress, and total_cycles is a UI counter, not the
            # DW round count. The DW script's max_rounds caps exploration rounds;
            # per_round is already bounded by parallel_workers to limit fan-out).
            pending: list[tuple[Path, str]] = []
            for i in range(len(investigate)):
                cycle_no = cycle_offset + i + 1
                fpath = d.joinpath("findings", "cycle_%03d.json" % cycle_no)
                meta, fin = investigate[i]
                label = str(meta.get("label", ""))
                insight = (
                    label[len("investigate: ") :] if label.startswith("investigate: ") else label
                )
                finding = {
                    "cycle": cycle_no,
                    "summary": _redact_llm(fin.get("result_summary", "")),
                    "key_insight": _redact_llm(insight),
                    "sources_checked": [],
                    "sources_empty": [],
                    "new_findings_count": 1,
                    "evidence_strength": "moderate",
                }
                pending.append((fpath, json.dumps(finding, indent=2)))
            if pending:

                def _write_and_persist_cycles() -> bool:
                    """Write new cycle files AND persist the count in ONE worker.

                    The bookkeeping rides in the same worker as the mutation so a
                    task cancellation delivered at an await cannot land between
                    them — the thread finishes both or neither, mirroring
                    ``_txn_and_notify``'s commit-then-notify discipline. Blocking;
                    call off-loop.
                    """
                    if not storage._write_new_cycle_files(pending):
                        return False
                    count = len(storage._list_cycle_files(campaign_id))
                    db = storage._get_db()
                    try:
                        db.execute("BEGIN")
                        # Predicated on the observed generation: even a poll that
                        # somehow raced past the entry check cannot write counts
                        # into a replacement run's row.
                        db.execute(
                            "UPDATE campaigns SET total_cycles=? " "WHERE id=? AND started_at IS ?",
                            (count, campaign_id, observed_started_at),
                        )
                        db.commit()
                    finally:
                        db.close()
                    return True

                # One worker hop for the whole batch, bookkeeping included.
                # Settled before cancellation can release the transition lock
                # (see _settle_before_cancellation).
                wrote = bool(
                    await lifecycle._settle_before_cancellation(
                        asyncio.create_task(asyncio.to_thread(_write_and_persist_cycles))
                    )
                )
            if wrote:
                lifecycle._emit_sse(
                    {
                        "type": "new_finding",
                        "campaign_id": campaign_id,
                        "finding": storage._read_finding_file(
                            storage._list_cycle_files(campaign_id)[-1]
                        ),
                    }
                )
            status = snap.get("status")
            if status == "finished":
                result = snap.get("result") if isinstance(snap.get("result"), dict) else {}
                report = str((result or {}).get("report") or "")
                if not report:
                    fs = (result or {}).get("findings") or []
                    report = "\n\n".join(str(x) for x in fs) if isinstance(fs, list) else ""
                await asyncio.to_thread(
                    storage._write_text,
                    d.joinpath("FINDINGS.md"),
                    _redact_llm(report) or "(no findings gathered)",
                )

                def _complete_and_notify() -> dict | None:
                    r = lifecycle._guarded_txn(
                        campaign_id,
                        CampaignStatus.COMPLETE,
                        (CampaignStatus.RUNNING,),
                        observed_started_at,
                    )
                    if r:
                        lifecycle._sse_from_thread(
                            event_loop, {"type": "complete", "campaign_id": campaign_id}
                        )
                    return r

                await asyncio.to_thread(_complete_and_notify)
            elif status in ("failed", "cancelled"):

                def _fail_and_notify() -> dict | None:
                    r = lifecycle._guarded_txn(
                        campaign_id,
                        CampaignStatus.FAILED,
                        (CampaignStatus.RUNNING,),
                        observed_started_at,
                        error_message=_redact_llm(
                            snap.get("error") or "workflow run ended without completing"
                        ),
                    )
                    if r:
                        lifecycle._sse_from_thread(
                            event_loop, {"type": "failed", "campaign_id": campaign_id}
                        )
                    return r

                await asyncio.to_thread(_fail_and_notify)
    except Exception:
        logger.exception("auto_research: workflow poll failed for %s", campaign_id)

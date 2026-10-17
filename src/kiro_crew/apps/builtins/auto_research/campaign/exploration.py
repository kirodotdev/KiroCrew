"""Recursive exploration for agent-mode campaigns.

Owns the emergent sub-question policy (ingest the worker's proposals ranked by
depth-decayed priority, de-duplicated and budgeted per round; activate them
into the checklist once the initial questions are addressed), the reserve zone
of trailing cycles kept for synthesis, and the one-shot FINALIZE MODE signal.
Each step runs off the event loop, on the watchdog's cycle-bookkeeping thread.
"""

from __future__ import annotations

import json
import logging
import math
import time

from kiro_crew.apps.builtins.auto_research import subquestion_queue as _sq
from kiro_crew.apps.builtins.auto_research.campaign import (
    LOGGER_NAME,
    lifecycle,
    publication,
    storage,
    untrusted,
)

logger = logging.getLogger(LOGGER_NAME)

_EMERGENT_FILENAME = "emergent_questions.json"
_FINALIZE_FLAG = "finalize.flag"


def _reserve_cycles(max_cycles: int, reserve_fraction: float) -> int:
    """Trailing cycles reserved for final synthesis (>=1 when bounded)."""
    if not max_cycles or max_cycles <= 0:
        return 0
    return max(1, math.ceil(max_cycles * max(0.0, min(1.0, reserve_fraction))))


def _in_reserve_zone(total_cycles: int, max_cycles: int, reserve_fraction: float) -> bool:
    """True once only the reserved trailing cycles remain — time to stop
    exploring and synthesize. Always False when max_cycles is unbounded (<=0)."""
    if not max_cycles or max_cycles <= 0:
        return False
    reserve = _reserve_cycles(max_cycles, reserve_fraction)
    return total_cycles >= max(1, max_cycles - reserve)


def _ingest_emergent_questions(campaign_id: str) -> list[dict]:
    """Admit agent-proposed emergent sub-questions into the queue (agent mode).

    Each cycle the agent MAY write ``emergent_questions.json`` = a JSON array of
    ``{"text", "priority"?}`` (findings-derived follow-ups). We rank by priority
    decayed for this round's depth, de-duplicate against the queue AND the
    existing checklist, admit at most ``max_subquestions_per_round`` into the
    queue's pending bucket, persist, and consume the file. Returns admitted items.
    """
    d = storage._safe_campaign_dir(campaign_id)
    if d is None:
        return []
    ef = d / _EMERGENT_FILENAME
    if not ef.exists():
        return []
    db = storage._get_db()
    row = db.execute(
        "SELECT execution_mode, max_subquestions_per_round, depth_decay, sub_questions "
        "FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    db.close()
    if row is None or row["execution_mode"] != storage.DEFAULT_EXECUTION_MODE:
        ef.unlink(missing_ok=True)  # not agent mode (or gone) — discard
        return []
    try:
        # LLM-authored sub-question text — non-ASCII is expected, so UTF-8 is
        # explicit and bad bytes are absorbed rather than aborting the sweep.
        raw = json.loads(ef.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        raw = []
    ef.unlink(missing_ok=True)  # consumed regardless of validity
    if not isinstance(raw, list) or not raw:
        return []
    max_admit = int(
        row["max_subquestions_per_round"]
        if row["max_subquestions_per_round"] is not None
        else storage.DEFAULT_MAX_SUBQUESTIONS_PER_ROUND
    )
    decay = float(
        row["depth_decay"] if row["depth_decay"] is not None else storage.DEFAULT_DEPTH_DECAY
    )
    existing = json.loads(row["sub_questions"] or "[]")
    existing_norm = {_sq.normalize(s.get("text", "")) for s in existing if isinstance(s, dict)}
    queue = _sq.load_queue(d)
    depth = _sq.next_depth(queue)
    factor = decay**depth

    # emergent_questions.json is LLM output that flows into the sub_questions DB
    # column and the dashboard UI — scrub creds + exfil URLs before it enters the
    # queue (same defense-in-depth the finding-read path applies).
    def _redact_em(s: str) -> str:
        cleaned, _ = untrusted.redact_credentials(s)
        cleaned, _ = untrusted.redact_exfiltration_urls(cleaned)
        return cleaned

    cands: list[dict] = []
    for it in raw:
        if isinstance(it, dict):
            text = str(it.get("text", "")).strip()
            base = float(it.get("priority", 0.5))
        else:
            text = str(it).strip()
            base = 0.5
        text = _redact_em(text)  # scrub LLM output before it reaches DB/UI
        if not text or _sq.normalize(text) in existing_norm:
            continue  # empty, or already a checklist question
        base = min(1.0, max(0.0, base))  # clamp to [0,1] before decay
        cands.append({"text": text, "priority": base * factor})
    admitted = _sq.enqueue(queue, cands, depth=depth, max_admit=max_admit)
    _sq.save_queue(d, queue)
    if admitted:
        lifecycle._audit("campaign_emergent_ingested", campaign_id)
    return admitted


def _activate_emergent(campaign_id: str) -> list[dict]:
    """Pull queued emergent sub-questions into the agent's checklist (agent mode).

    Gate: only once the initial (grill/manual) questions are addressed — either
    all marked answered, or enough cycles have run to have plausibly covered them
    (``total_cycles >= #initial``), since 'answered' status is not always set.
    Dequeues up to ``max_subquestions_per_round`` highest-priority pending items,
    appends them to ``sub_questions`` (origin 'emergent', status 'open'), marks
    them analyzed (dedup ledger), and rewrites the brief. Returns activated items.
    """
    d = storage._safe_campaign_dir(campaign_id)
    if d is None:
        return []
    queue = _sq.load_queue(d)
    if _sq.pending_count(queue) == 0:
        return []
    with publication._brief_publish_lock(campaign_id):
        db = storage._get_db()
        # Write lock BEFORE the read: this is a read-modify-write on sub_questions
        # (same shape as _append_question), so two concurrent writers must
        # serialize instead of both reading the same base list.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT execution_mode, max_subquestions_per_round, sub_questions, total_cycles "
            "FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        if row is None or row["execution_mode"] != storage.DEFAULT_EXECUTION_MODE:
            db.execute("ROLLBACK")
            db.close()
            return []
        subs = json.loads(row["sub_questions"] or "[]")
        initial = [
            s
            for s in subs
            if isinstance(s, dict) and s.get("origin") in ("grill", "manual", None, "")
        ]
        initial_open = [s for s in initial if s.get("status") != "answered"]
        if initial_open and int(row["total_cycles"] or 0) < len(initial):
            db.execute("ROLLBACK")
            db.close()
            return []  # still working the initial questions — hold emergent ones
        k = int(
            row["max_subquestions_per_round"]
            if row["max_subquestions_per_round"] is not None
            else storage.DEFAULT_MAX_SUBQUESTIONS_PER_ROUND
        )
        activated = _sq.dequeue_top_k(queue, k)
        if not activated:
            db.execute("ROLLBACK")
            db.close()
            return []
        for a in activated:
            subs.append({"text": a["text"], "origin": "emergent", "status": "open"})
        db.execute(
            "UPDATE campaigns SET sub_questions = ? WHERE id = ?",
            (json.dumps(subs), campaign_id),
        )
        # Re-read inside the transaction; publish AFTER commit under the publish
        # lock (mirrors _append_question / the launch path): the brief on disk
        # always reflects a COMMITTED row, publish order matches commit order, and
        # a rollback can never leave a brief describing phantom state.
        full = db.execute(
            "SELECT question, sub_questions, sources, scope_constraints, max_cycles, "
            "idle_secs, success_criteria, auto_approve, parallel_workers "
            "FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        db.commit()
        db.close()
        # Ledger BEFORE the brief publish (both inside the publish lock): the
        # dedup ledger must record the activation even if the brief write then
        # fails — otherwise the items stay pending and are re-activated
        # (duplicated) on the next cycle.
        _sq.mark_analyzed(queue, activated)  # dedup ledger: never re-admit/re-activate
        _sq.save_queue(d, queue)
        if full is not None:
            publication._write_brief(campaign_id, full)  # surface the new emergent items next cycle
    lifecycle._audit("campaign_emergent_activated", campaign_id)
    return activated


def _should_finalize(campaign_id: str) -> bool:
    """Agent-mode: are we in the reserved trailing cycles (stop exploring, start
    synthesizing)? Reads max_cycles + reserve_fraction + total_cycles."""
    db = storage._get_db()
    row = db.execute(
        "SELECT execution_mode, max_cycles, reserve_fraction, total_cycles "
        "FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    db.close()
    if row is None or row["execution_mode"] != storage.DEFAULT_EXECUTION_MODE:
        return False
    reserve_fraction = (
        float(row["reserve_fraction"])
        if row["reserve_fraction"] is not None
        else storage.DEFAULT_RESERVE_FRACTION
    )
    return _in_reserve_zone(
        int(row["total_cycles"] or 0), int(row["max_cycles"] or 0), reserve_fraction
    )


def _enter_finalize(campaign_id: str) -> bool:
    """Signal FINALIZE MODE once: freeze exploration (drop any stray emergent
    file) and write a guidance directive telling the agent to consolidate the
    accumulated findings into a final answer. Returns True if newly signaled."""
    d = storage._safe_campaign_dir(campaign_id)
    if d is None:
        return False
    (d / _EMERGENT_FILENAME).unlink(missing_ok=True)  # halt pending exploration
    flag = d / _FINALIZE_FLAG
    if flag.exists():
        return False  # already signaled — leave the guidance in place
    flag.write_text(str(time.time()), encoding="utf-8")
    storage.write_guidance(
        campaign_id,
        "FINALIZE MODE — you are near the cycle budget. STOP opening new "
        "sub-questions and STOP proposing emergent_questions.json. Use the "
        "remaining cycles to CONSOLIDATE everything you have learned into a "
        "clear, well-structured final answer to the main question in FINDINGS.md "
        "(executive summary, key findings with evidence, and any open gaps). If "
        "the Definition of Done is met, set verification.passed=true in your finding.",
    )
    lifecycle._audit("campaign_finalize_mode", campaign_id)
    return True


def _advance_exploration(campaign_id: str) -> None:
    """One recursive-exploration step (agent mode). When the campaign enters the
    reserved trailing cycles, freeze exploration and signal FINALIZE MODE so the
    run still delivers a synthesized report instead of exploring up to the cap;
    otherwise ingest agent-proposed emergent sub-questions and activate queued
    ones. Best-effort — never raises into the watchdog.
    """
    try:
        if _should_finalize(campaign_id):
            _enter_finalize(campaign_id)
            return
        _ingest_emergent_questions(campaign_id)
        _activate_emergent(campaign_id)
    except Exception:
        logger.exception("auto_research: emergent exploration failed for %s", campaign_id)

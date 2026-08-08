"""Cron job and Lessons CRUD API handlers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.cron import CronStoreBusy, is_valid_timezone
from kiro_crew.dashboard.cron_inject import (
    hydrate_slot_from_history,
    inject_cron_result_to_dashboard,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import (
    _MODEL_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    LEARN_ADD_SCHEMA,
    MAX_MEDIUM_STRING,
    MAX_SHORT_STRING,
    SLACK_THREAD_TS_RE,
    ValidationError,
    validate_string_field,
    validate_tool_args,
)

from ._shared import (
    _blocks_reads_session,
    _get_active_workspace,
    _get_lessons,
    _get_memory,
    _is_restricted_session,
    _session_has_persisted_history,
)

logger = logging.getLogger(__name__)

# 409 Conflict body returned when a cron-store mutator times out waiting for the
# store lock (CronStoreBusy). Contention is transient (a large atomic save on
# network storage, the CLI process, or the off-loop batch worker), so the client
# should retry rather than treat it as a hard failure. See CronService mutators.
_CRON_BUSY_STATUS = 409
_CRON_BUSY_BODY = {"error": "cron store busy, please retry", "retryable": True}


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


_CONTRADICTION_PROMPT = (
    "Given an OLD rule and a NEW rule, determine if they contradict each other "
    "(following both simultaneously is impossible or produces conflicting behavior).\n\n"
    "OLD: {old_rule}\n\nNEW: {new_rule}\n\n"
    "Respond with exactly one word: CONTRADICTORY, COMPLEMENTARY, or UNRELATED."
)

_CONTRADICTION_MODEL = "auto"  # inherit the governed default; a hardcoded id 400s where unavailable
# Per-candidate cap on the background contradiction verdict. The sweep runs
# fire-and-forget after the lesson is already persisted, so this bounds a hung
# model call rather than gating the write path.
_CONTRADICTION_TIMEOUT = 60.0


async def _classify_contradiction(state: DashboardState, prompt: str) -> str:
    """Run one contradiction classification on the shared ``_bg`` runtime.

    Mirrors the lightweight background path used by title/suggestion generation
    (``chat_title`` / ``suggestions``): acquire an ephemeral ``_bg`` session
    handle via ``get_bg_session``, best-effort pin it to the cheap model, stream
    to completion while rejecting any tool call (the classification is
    tool-free), then always ``destroy()`` the handle. A fresh handle per call
    keeps each verdict a clean binary classification — no cross-candidate turn
    history — and avoids the unbounded context growth of a single long-lived
    session reused across every ``learn_add``. Bounded by
    ``_CONTRADICTION_TIMEOUT``. Returns the first upper-cased token of the
    model's reply (e.g. ``"CONTRADICTORY"``), or ``""`` on empty output.
    """
    text = await run_bg_oneliner(
        state.sessions,
        prompt,
        model=_CONTRADICTION_MODEL,
        sel_source="contradiction_check",
        timeout=_CONTRADICTION_TIMEOUT,
    )
    stripped = text.strip()
    return stripped.upper().split()[0] if stripped else ""


async def _resolve_contradictions(
    state: DashboardState, new_rule: str, candidates: list[dict]
) -> list[str]:
    """Use an LLM to identify which candidate lessons contradict the new rule.

    Each candidate is classified independently on a fresh ``_bg`` runtime
    session (see ``_classify_contradiction``). A per-candidate failure/timeout
    is swallowed so one bad verdict never aborts the sweep — the lesson is
    already persisted, and a missed verdict self-heals on the next ``learn_add``
    touching the topic.
    """
    to_delete: list[str] = []
    for candidate in candidates:
        prompt = _CONTRADICTION_PROMPT.format(
            old_rule=candidate["rule"], new_rule=new_rule
        )
        try:
            verdict = await _classify_contradiction(state, prompt)
        except Exception:
            logger.debug("Contradiction check failed for %r", candidate["key"], exc_info=True)
            continue
        if verdict == "CONTRADICTORY":
            logger.info(
                "Contradiction: new %r supersedes %r (sim=%.2f)",
                new_rule[:60], candidate["rule"][:60], candidate["similarity"],
            )
            to_delete.append(candidate["key"])
    return to_delete


async def _resolve_and_supersede(
    state: DashboardState, sk: str, rule: str, candidates: list[dict], vs: Any
) -> None:
    """Resolve contradictions and delete superseded lessons (runs in background).

    Split out of ``api_lessons_create`` so the slow per-candidate LLM verdict
    does not block the HTTP response. Deletes are emitted with the same SEL
    audit event as the inline path. Exceptions are swallowed (logged) —
    a failed background sweep must never crash the event loop, and the lesson
    itself is already persisted.
    """
    try:
        contradicted = await _resolve_contradictions(state, rule, candidates)
    except Exception:
        # Outer guard: this runs as a fire-and-forget background task, so an
        # unhandled raise would surface only as a noisy "Task exception was never
        # retrieved" — and the lesson is already persisted (not data loss). warning,
        # not debug: a persistent sweep failure means contradicted lessons accumulate
        # uncleaned, and running in the background means no request timeout surfaces
        # the failure — operators need the visibility.
        logger.warning("Background contradiction sweep failed", exc_info=True)
        return
    for key in contradicted:
        try:
            # Audit the supersede DECISION *before* the destructive delete: a
            # lesson must never be deleted without a SEL record, so if the audit
            # call itself raises (audit-service blip) we skip the delete for this
            # key rather than deleting unaudited.
            _sel().log_api_access(
                caller=sk, operation="lesson.contradiction_superseded",
                outcome="allowed", source="dashboard", resources=key,
            )
            # delete_semantic is a sync FAISS op; off-load so this background
            # sweep doesn't block concurrent dashboard/Slack requests on the loop.
            await asyncio.to_thread(vs.delete_semantic, key, "contradiction_superseded")
            logger.info("Deleted contradicted lesson: %s", key)
        except Exception:
            # per-key so one bad/already-deleted key doesn't abort the batch (a
            # concurrent sweep may have deleted it — candidates are a write-time snapshot).
            logger.warning("Failed to supersede contradicted lesson %s", key, exc_info=True)
            continue


# ── Cron / Lessons ──


async def api_crons_create(request: web.Request) -> web.Response:
    """POST /api/crons — create a cron job."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    # Type-validate every string field BEFORE calling string methods on it.
    # A JSON array/dict/int in these fields would otherwise raise AttributeError
    # (.strip() on a non-str) -> HTTP 500. validate_string_field enforces
    # isinstance(str), sanitizes, and bounds length, mirroring the MCP
    # CRON_ADD_SCHEMA so the REST + tool paths validate identically.
    try:
        name = validate_string_field(body, "name", required=True, max_len=MAX_SHORT_STRING)
        message = validate_string_field(body, "message", max_len=MAX_MEDIUM_STRING)
        schedule = validate_string_field(body, "schedule", max_len=100)
        cron_expr = validate_string_field(body, "cron", max_len=100) or None
        channel = validate_string_field(body, "channel", max_len=CHANNEL_MAX_LEN) or None
        approval_mode = validate_string_field(body, "approval_mode", max_len=10)
        timezone_val = validate_string_field(body, "timezone", max_len=50)
        agent_id = validate_string_field(body, "agent", max_len=MAX_SHORT_STRING)
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if not name or not message:
        return web.json_response({"error": "name and message required"}, status=400)
    every = body.get("every")
    if not every and not cron_expr and schedule:
        # Treat schedule string as cron expr if 5-field, else as interval
        cron_expr = schedule if len(schedule.split()) == 5 else None
    if channel and not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    if approval_mode and approval_mode not in {"", "auto"}:
        return web.json_response({"error": "invalid approval_mode"}, status=400)
    silent = body.get("silent", False)
    if timezone_val and not is_valid_timezone(timezone_val):
        safe_tz, _ = redact_credentials(redact_exfiltration_urls(timezone_val)[0])
        return web.json_response({"error": f"invalid timezone: {safe_tz!r}"}, status=400)
    strict_schedule = body.get("strict_schedule", False)
    hide_in_chat = body.get("hide_in_chat", False)
    # Same folder_id contract as PATCH /api/crons/{id}: string or null → "",
    # anything else is a 400 so the two entry points cannot diverge.
    folder_id = body.get("folder_id", "")
    if folder_id is None:
        folder_id = ""
    elif not isinstance(folder_id, str) or len(folder_id) > MAX_SHORT_STRING:
        return web.json_response(
            {"error": "invalid folder_id format", "code": "invalid_folder_id"},
            status=400,
        )
    # Validate model BEFORE add_job so an invalid value never leaves an
    # orphaned job behind (a retried create would then duplicate it).
    model_raw = body.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        # A numeric/bool JSON `model` would raise AttributeError on .strip()
        # (HTTP 500); reject it as a clean 400 instead.
        return web.json_response({"error": "invalid model format"}, status=400)
    model_val = (model_raw or "").strip()
    if model_val:
        if len(model_val) > MAX_SHORT_STRING or not _MODEL_NAME_RE.match(model_val):
            return web.json_response({"error": "invalid model format"}, status=400)
        # No membership gate: the model dropdown is sourced from the live
        # kiro-cli `--list-models` (via /api/models), not the claude_code
        # registry family, so any well-formed id the CLI advertises is valid.
        # Matches the chat model path (which also skips membership); the
        # runtime is model-agnostic with a gateway fallback. Only normalize
        # the "auto" inherit sentinel below.
        resolved_model = model_registry.to_provider_id(model_val, "claude_code")
        if resolved_model == "":
            # "auto" sentinel (canonical key with no pinned provider id):
            # explicit inherit — same as leaving model unset.
            model_val = ""
    # Build the job FULLY-FORMED in a single locked add_job_async transaction.
    # Passing every optional field into the locked build+persist (rather than
    # mutating the returned job and calling a bare, unlocked `_save()`) closes
    # the data-loss race: two concurrent creates could interleave at the
    # `await`, and the unlocked save could overwrite the other request's job.
    add_kwargs: dict[str, Any] = {
        "channel": channel,
        "agent_id": (agent_id or ""),
        "model": model_val,
        "silent": bool(silent),
        "timezone": (timezone_val or ""),
        "strict_schedule": bool(strict_schedule),
        "hide_in_chat": bool(hide_in_chat),
        "folder_id": folder_id,
    }
    if approval_mode:
        add_kwargs["approval_mode"] = approval_mode
    if every:
        try:
            every = int(every)
        except (ValueError, TypeError):
            return web.json_response({"error": "'every' must be an integer"}, status=400)
        try:
            job = await state.crons.add_job_async(name, message, every_secs=every, **add_kwargs)
        except CronStoreBusy:
            return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    elif cron_expr:
        try:
            job = await state.crons.add_job_async(
                name, message, cron_expr=cron_expr, **add_kwargs
            )
        except CronStoreBusy:
            return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    else:
        return web.json_response({"error": "schedule, every, or cron required"}, status=400)
    state.push_refresh("crons")
    return web.json_response({"ok": True, "id": job.id})


async def api_cron_delete(request: web.Request) -> web.Response:
    """DELETE /api/crons/{id} — remove a cron job."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        ok = await state.crons.remove_job_async(job_id)
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    if ok:
        await state.crons.get_history().delete_job_history(job_id)
        state.push_refresh("crons")
    return web.json_response({"ok": ok})


# Guardrail: cap batch size so a runaway/hostile payload can't pin the event
# loop deleting thousands of jobs (each remove_job is a sync save + async
# history delete). 500 comfortably exceeds any realistic schedule list.
_MAX_BATCH_DELETE = 500


async def api_cron_batch_delete(request: web.Request) -> web.Response:
    """DELETE /api/crons — remove multiple cron jobs in one call.

    Body: ``{"ids": ["<job_id>", ...]}``. Each id is removed independently so a
    missing/already-deleted id (e.g. a stale UI selection, or a concurrent
    delete) lands in ``failed`` rather than aborting the whole batch. History is
    purged per successfully-removed job, mirroring the single-delete path, and a
    single ``crons`` refresh is pushed after the batch instead of one per id.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return web.json_response({"error": "ids must be a non-empty array"}, status=400)
    if not all(isinstance(i, str) for i in ids):
        return web.json_response({"error": "ids must be an array of strings"}, status=400)
    # De-duplicate while preserving order (a select-all + click race can send dupes).
    unique_ids = list(dict.fromkeys(ids))
    if len(unique_ids) > _MAX_BATCH_DELETE:
        return web.json_response(
            {"error": f"too many ids (max {_MAX_BATCH_DELETE})"}, status=400
        )
    deleted: list[str] = []
    failed: list[str] = []
    try:
        # remove_jobs runs the WHOLE batch under one file lock with one
        # reload/serialize/save — and offloads that disk work to a worker
        # thread (no-blocking-call-on-event-loop; slow/network storage would
        # otherwise stall chat + heartbeat). Only its _arm_timer() step runs
        # back on the loop (asyncio.create_task needs it): moving it off-loop
        # would raise AFTER the on-disk delete and leave the scheduler timer
        # cancelled.
        deleted, failed = await state.crons.remove_jobs(unique_ids)
    except Exception:
        # The batch itself raised (unexpected) — report everything as failed.
        logger.warning("Batch delete failed", exc_info=True)
        failed = unique_ids
        deleted = []
    for job_id in deleted:
        # The job is gone now, so it is unconditionally a successful delete.
        # History cleanup is best-effort: a failure there must NOT reclassify a
        # completed delete as "failed" — that would make the UI offer a retry
        # that can never succeed (the job no longer exists).
        try:
            await state.crons.get_history().delete_job_history(job_id)
        except Exception:
            logger.warning(
                "History cleanup failed for cron %s (job already removed)",
                job_id, exc_info=True,
            )
    # SEL audit: a destructive batch action must record who/what/when + the
    # affected resources and outcome. Cron IDs are non-sensitive (no
    # creds/PII), so logging them is compliant.
    _sel().log_api_access(
        caller="dashboard",
        operation="cron.batch_delete",
        outcome="ok" if deleted else "failed",
        source="api_cron_batch_delete",
        resources=f"requested={unique_ids} deleted={deleted} failed={failed}",
    )
    if deleted:
        state.push_refresh("crons")
    # ok reflects whether anything was actually deleted — consistent with the
    # single-delete endpoint (ok:false when the job didn't exist) and the audit
    # line above, so callers can detect a fully-failed batch without inspecting
    # the arrays.
    return web.json_response({"ok": len(deleted) > 0, "deleted": deleted, "failed": failed})


async def api_cron_update(request: web.Request) -> web.Response:
    """PATCH /api/crons/{id} — update a cron job (partial)."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    kwargs: dict[str, Any] = {}
    for key in (
        "name",
        "message",
        "channel",
        "approval_mode",
        "silent",
        "strict_schedule",
        "hide_in_chat",
        "folder_id",
    ):
        if key in body:
            kwargs[key] = body[key]
    # folder_id must be a string (or null → ""): a non-string JSON value
    # would be persisted verbatim into the schema and corrupt reads.
    if "folder_id" in kwargs:
        fid = kwargs["folder_id"]
        if fid is None:
            kwargs["folder_id"] = ""
        elif not isinstance(fid, str) or len(fid) > MAX_SHORT_STRING:
            return web.json_response(
                {"error": "invalid folder_id format", "code": "invalid_folder_id"},
                status=400,
            )
    # UI sends "agent"; internal kwarg is "agent_id". Accept "agent_id" for scripted callers.
    # Normalize whitespace and coerce null so update and create persist the same value.
    if "agent" in body:
        kwargs["agent_id"] = (body["agent"] or "").strip()
    elif "agent_id" in body:
        kwargs["agent_id"] = (body["agent_id"] or "").strip()
    if "model" in body:
        model_raw = body["model"]
        if model_raw is not None and not isinstance(model_raw, str):
            # Non-string JSON `model` would raise on .strip() (HTTP 500) — 400.
            return web.json_response({"error": "invalid model format"}, status=400)
        m = (model_raw or "").strip()
        if m:
            if len(m) > MAX_SHORT_STRING or not _MODEL_NAME_RE.match(m):
                return web.json_response({"error": "invalid model format"}, status=400)
            # No membership gate: the model dropdown is sourced from the live
            # kiro-cli `--list-models` (via /api/models), not the claude_code
            # registry family, so any well-formed id the CLI advertises is
            # valid. Matches the chat model path (which also skips membership);
            # the runtime is model-agnostic with a gateway fallback. Only
            # normalize the "auto" inherit sentinel below.
            resolved_model = model_registry.to_provider_id(m, "claude_code")
            if resolved_model == "":
                m = ""
        kwargs["model"] = m
    # Validate channel if being updated
    if "channel" in kwargs:
        ch = (kwargs["channel"] or "").strip() or None
        kwargs["channel"] = ch
        if ch and (len(ch) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(ch)):
            return web.json_response({"error": "invalid channel ID format"}, status=400)
    # Schedule: accept cron_expr or every (seconds)
    if "cron" in body:
        kwargs["cron_expr"] = body["cron"]
    if "every" in body:
        kwargs["every_secs"] = body["every"]
    if "timezone" in body:
        tz_val = (body["timezone"] or "").strip()
        if tz_val and not is_valid_timezone(tz_val):
            safe_tz, _ = redact_credentials(redact_exfiltration_urls(tz_val)[0])
            return web.json_response({"error": f"invalid timezone: {safe_tz!r}"}, status=400)
        kwargs["timezone"] = tz_val
    if not kwargs:
        return web.json_response({"error": "no fields to update"}, status=400)
    try:
        job = await state.crons.update_job_async(job_id, **kwargs)
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    state.push_refresh("crons")
    return web.json_response({"ok": True, "id": job.id})


async def api_cron_run(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/run — trigger immediate execution."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    # Freshness-guaranteed lookup: this endpoint is handed a job id minted by
    # ANOTHER process (`kirocrew cron add`, the MCP cron_add tool), which writes
    # crons.json directly. The cache-only `list_jobs()` would not see that job
    # until the timer tick refreshes the in-memory snapshot (≤_TIMER_POLL_SECS),
    # so triggering a just-created job 404'd for up to that long. Same rationale
    # as the GET handler below; the read runs in a worker thread, so the loop is
    # not blocked.
    job = await state.crons.get_job_async(job_id)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    # Reject if a run is already in flight. Overwriting _running_tasks[job_id]
    # would orphan the prior task's handle (it could no longer be
    # tracked/cancelled/joined) and allow overlapping duplicate runs. The
    # check-and-set below is atomic: there is no await between the guard and the
    # assignment, so the single-threaded event loop cannot interleave a second
    # request into this critical section. (The lookup above awaits, so two
    # concurrent requests can both reach the guard — but only one can pass it,
    # because the guard and the assignment are not separated by an await.)
    if job_id in state.crons._running_tasks or state.crons.is_running(job_id):
        return web.json_response({"error": "job is already running"}, status=409)
    task = asyncio.create_task(state.crons.run_job(job_id))  # type: ignore[arg-type]
    state.crons._running_tasks[job_id] = task  # type: ignore[assignment]

    def _on_done(t: asyncio.Task, _jid: str = job_id) -> None:  # type: ignore[type-arg]
        if state.crons._running_tasks.get(_jid) is t:
            state.crons._running_tasks.pop(_jid, None)

    task.add_done_callback(_on_done)
    state.push_refresh("crons")
    safe_name = redact_credentials(redact_exfiltration_urls(job.name)[0])[0]
    return web.json_response({"ok": True, "name": safe_name})


async def api_cron_cancel(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/cancel — cancel a running execution."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    jobs = state.crons.list_jobs(include_disabled=True)
    job = next((j for j in jobs if j.id == job_id), None)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    cancelled = await state.crons.cancel(job_id)
    if not cancelled:
        return web.json_response({"error": "job is not running"}, status=409)
    state.push_refresh("crons")
    safe_name = redact_credentials(redact_exfiltration_urls(job.name)[0])[0]
    return web.json_response({"ok": True, "name": safe_name})


async def api_cron_to_chat(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/to-chat — open last result in a chat session."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    slot_name = f"cron-{job_id}"
    jobs = state.crons.list_jobs(include_disabled=True)
    job = next((j for j in jobs if j.id == job_id), None)
    if job:
        history = (
            await asyncio.to_thread(state.conversation_log.read_messages, f"cron:{job.id}")
            if state.conversation_log else []
        )
        inject_cron_result_to_dashboard(state, job, job.last_result or "", history=history)
    else:
        # Job deleted (one-shot with delete_after_run). Create slot from history or notification.
        session_key = f"cron:{job_id}"
        history = (
            await asyncio.to_thread(state.conversation_log.read_messages, session_key)
            if state.conversation_log else []
        )
        if history:
            slot = state.get_or_create_slot(name=slot_name, agent="")
            if not slot.linked_session_key:
                slot.linked_session_key = session_key
                hydrate_slot_from_history(slot, history)
        else:
            # No session log — fall back to notification body.
            notif = next(
                (n for n in state._notification_log if n.get("job_id") == job_id),
                None,
            )
            if not notif:
                return web.json_response({"error": "job not found"}, status=404)
            slot = state.get_or_create_slot(name=slot_name, agent="")
            body = notif.get("body", "")
            if body:
                body, _ = redact_exfiltration_urls(body)
                body, _ = redact_credentials(body)
                if not any(message.get("content") == body for message in slot.messages):
                    slot.append("assistant", body, "msg msg-a")
        state.push_slots_update()
    return web.json_response({"ok": True, "slot": slot_name})


async def api_cron_enable(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/enable — toggle enable/disable."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = body.get("enabled", True)
    try:
        ok = await state.crons.enable_job_async(job_id, enabled=enabled)
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    if ok:
        state.push_refresh("crons")
    return web.json_response({"ok": ok})


async def api_cron_ack(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/ack — acknowledge a cron notification."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    summary = body.get("summary", "acknowledged")
    notification_ts = body.get("ts", "")
    try:
        ok = await state.crons.ack_job_async(job_id, summary)
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    if notification_ts:
        await state.ack_notification(notification_ts)
    return web.json_response({"ok": ok})


async def api_cron_history(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/history — paginated execution history (no trace)."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        limit = int(request.query.get("limit", "20"))
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        offset = 0
    runs, total = await state.crons.get_history().get_job_history(job_id, limit=limit, offset=offset)
    for run in runs:
        for key in ("summary", "error"):
            if run.get(key):
                run[key] = redact_credentials(redact_exfiltration_urls(run[key])[0])[0]
    return web.json_response({"runs": runs, "total": total})


async def api_cron_history_detail(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/history/{run_id} — full run detail with trace."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    run_id = request.match_info["run_id"]
    detail = await state.crons.get_history().get_run_detail(job_id, run_id)
    if not detail:
        return web.json_response({"error": "run not found"}, status=404)
    for key in ("summary", "trace", "error"):
        if detail.get(key):
            detail[key] = redact_credentials(redact_exfiltration_urls(detail[key])[0])[0]
    return web.json_response(detail)


async def api_cron_history_all(request: web.Request) -> web.Response:
    """GET /api/crons/history — unified history across all jobs, enriched with job_name."""
    state: DashboardState = request.app["state"]
    job_id = request.query.get("job_id")
    try:
        limit = int(request.query.get("limit", "20"))
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        offset = 0
    runs, total = await state.crons.get_history().get_all_history(
        job_id=job_id, limit=limit, offset=offset
    )
    # Enrich with job_name
    jobs_by_id = {j.id: j for j in state.crons.list_jobs(include_disabled=True)}
    for run in runs:
        jid = run.get("job_id", "")
        job = jobs_by_id.get(jid)
        run["job_name"] = job.name if job else jid
        for key in ("job_name", "summary", "trace", "error"):
            if run.get(key):
                run[key] = redact_credentials(redact_exfiltration_urls(run[key])[0])[0]
    return web.json_response({"runs": runs, "total": total})


async def api_lessons_create(request: web.Request) -> web.Response:
    """POST /api/lessons — add a lesson (vector store or JSONL fallback)."""
    from kiro_crew.learn import Lesson  # noqa: F811

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    # Block lesson writes from restricted (incognito/temporary/guest) sessions.
    sk = request.headers.get("X-Session-Key", "")
    if not sk:
        _sel().log_api_access(
            caller="anonymous", operation="learn_add", outcome="denied",
            source="dashboard", resources="missing_session_key",
        )
        return web.json_response({"error": "missing X-Session-Key"}, status=400)
    if sk != "dashboard:ui":
        slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
        in_slots = slot_name in state._slots
        in_restricted = sk in state._restricted_keys
        # A channel-originated session (Slack, Telegram, Discord, Webex,
        # WeCom, …) is a legitimate established session: its key is namespaced
        # ``{channel}:{conversation_id}`` and the transport publishes
        # ``session_pid`` so the gateway resolves this X-Session-Key (#232).
        # Recognise the WHOLE channel-namespace family via the canonical
        # ``is_channel_session_key`` — not just Slack. Two reasons this is the
        # right gate, both already true for Slack:
        #   * the first learn_add in a fresh channel thread races the JSONL
        #     flush (which only lands after the LLM turn completes), so a
        #     namespace fast-path avoids a spurious HTTP 400 until the
        #     transcript is on disk; and
        #   * the ``_session_has_persisted_history`` fallback below cannot
        #     rescue a channel key anyway — ``slot_name`` is
        #     ``sk.split(":", 1)[-1]`` (inner colons kept, channel prefix
        #     dropped) while the file is ``dashboard_<safe_key>.jsonl`` with
        #     colons folded to ``_``, so no probed name ever matches.
        # Before this, only ``slack:`` was accepted, so learn_add failed with
        # HTTP 400 "unknown session" from every OTHER channel (Telegram /
        # Discord / Webex / WeCom) even though the session is fully identified
        # (#1268). The bare Slack thread_ts shim stays for legacy native-Slack
        # keys. Incognito/temporary sessions are still blocked below by
        # ``_is_restricted_session`` (Slack is the only channel with that
        # concept), so widening the namespace does not widen memory writes to
        # ephemeral sessions.
        is_channel_ns = is_channel_session_key(sk) or bool(SLACK_THREAD_TS_RE.match(sk))
        # Only consult the on-disk JSONL when the cheaper in-memory
        # checks all fail. ``_session_has_persisted_history()`` performs
        # synchronous filesystem I/O (up to two ``Path.exists()`` calls),
        # so evaluating it eagerly on every ``learn_add`` request would
        # block the event loop on the common (live-slot) path. Deferring
        # it keeps the fallback semantics identical while making the
        # happy path allocation-free.
        if not (in_slots or in_restricted or is_channel_ns):
            if not _session_has_persisted_history(slot_name):
                # Slot may have been evicted from memory (idle sweep,
                # gateway restart) while the MCP subprocess keeps its
                # original KIROCREW_SESSION_KEY env var. Ephemeral
                # (incognito/temporary) sessions never write JSONL, so
                # the absence of a session JSONL here means the key
                # genuinely does not belong to any established session.
                _sel().log_api_access(
                    caller=sk, operation="learn_add", outcome="denied",
                    source="dashboard", resources="unknown_session",
                )
                return web.json_response({"error": "unknown session"}, status=400)
            # JSONL-fallback is the sole reason the call is permitted.
            # Audit it as an allow decision so session-recovery
            # authorization is traceable alongside the deny path above.
            _sel().log_api_access(
                caller=sk, operation="learn_add", outcome="allowed",
                source="dashboard", resources="jsonl_fallback_recovery",
            )
        elif in_slots:
            # Live in-memory slot — the common happy path. Audit so that
            # every ``learn_add`` permission decision on this branch is
            # traceable.
            _sel().log_api_access(
                caller=sk, operation="learn_add", outcome="allowed",
                source="dashboard", resources="live_slot",
            )
        elif in_restricted:
            _sel().log_api_access(
                caller=sk, operation="learn_add", outcome="allowed",
                source="dashboard", resources="restricted_key",
            )
        else:  # is_channel_ns
            _sel().log_api_access(
                caller=sk, operation="learn_add", outcome="allowed",
                source="dashboard", resources="channel_namespace",
            )
    else:
        # Browser UI's static key — implicitly trusted, but the allow
        # decision itself is still an authorization outcome and must be
        # audited (every permission decision emits a SEL event).
        _sel().log_api_access(
            caller=sk, operation="learn_add", outcome="allowed",
            source="dashboard", resources="dashboard_ui",
        )
    if _is_restricted_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        logger.warning("Blocked learn_add from restricted session %s", sk)
        _sel().log_api_access(
            caller=sk,
            operation="learn_add",
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
            error="Memory writes are not allowed in this session mode.",
        )
        return web.json_response(
            {"error": "Memory writes are not allowed in this session mode."},
            status=403,
        )
    # Validate body fields against the SAME schema the learn_add MCP tool uses
    # (LEARN_ADD_SCHEMA), so REST and tool paths share one source of truth:
    # rule must be a string (bounded to MAX_SHORT_STRING), category/scope are
    # enum-restricted, workspace is pattern-checked. A non-string
    # rule (array/dict) would otherwise raise AttributeError on .strip() -> HTTP 500, and
    # category/length would be unbounded. Only schema-known keys are validated so
    # unrelated body fields don't trip unknown-field rejection.
    known = {f.name for f in LEARN_ADD_SCHEMA.fields}
    try:
        cleaned = validate_tool_args(
            {k: v for k, v in body.items() if k in known}, LEARN_ADD_SCHEMA
        )
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    rule = cleaned["rule"]
    if not rule:
        return web.json_response({"error": "rule is required"}, status=400)
    category = cleaned.get("category", "knowledge")
    scope = cleaned.get("scope", "global")
    # LEARN_ADD_SCHEMA accepts and validates ``negative``, but both write paths
    # below discarded it -- write_lesson got a literal None and the JSONL Lesson
    # omitted the kwarg -- so every NOT-clause sent to this route, from the
    # learn_add MCP tool, the dashboard, or the CLI, was silently lost.
    negative = cleaned.get("negative") or None
    # Write to vector store if available, else JSONL
    vs = _get_memory(state).vector_store
    if vs:
        # Embed the rule once off the event loop and reuse it for both the
        # contradiction scan and write_lesson's own dedup pass — the store
        # methods otherwise each perform a blocking in-process embed of the same
        # text. find_contradiction_candidates and write_lesson are synchronous
        # (blocking embed + O(N) cosine scan), so run them via to_thread to
        # avoid stalling concurrent dashboard/Slack requests.
        # Read the space generation BEFORE embedding: write_lesson cannot infer the
        # space of a vector computed out here, and a model swap landing between this
        # embed and the write would otherwise commit it into the wrong space.
        rule_emb_generation = vs.space_generation
        rule_emb = await asyncio.to_thread(vs.embed_lesson, rule)
        # Persist the lesson immediately so the request returns fast. The
        # contradiction sweep below makes a per-candidate LLM call (~27s each);
        # running it inline would exceed the MCP client's 30s timeout while the
        # write still completed server-side, so the caller would see a "timeout"
        # for a lesson that was actually saved (and re-saved on every retry).
        # Writing first, then sweeping in the background, keeps the slow LLM call
        # off the request path.
        wrote = await asyncio.to_thread(
            vs.write_lesson,
            rule,
            category,
            negative,
            "user_explicit",
            rule_emb,
            rule_emb_generation,
        )
        # Sweep ONLY when the lesson actually landed. write_lesson returns False
        # for a value its preflight refuses (reachable now that ``negative`` is
        # forwarded here at all -- this call site passed a literal None before) and
        # for a dedup refusal. The return value used to be discarded, so a refused
        # write still ran the sweep below, and _resolve_and_supersede would
        # delete_semantic an older contradicted lesson whose "replacement" was never
        # stored -- destroying a lesson on a request that persisted nothing, under
        # HTTP 200. Superseding on the authority of a write that did not happen is
        # wrong for BOTH False cases, so gate on the result rather than the cause.
        if wrote:
            candidates = await asyncio.to_thread(
                vs.find_contradiction_candidates, rule, 0.4, 0.85, rule_emb
            )
            if candidates:
                # Fire-and-forget via this module's _background_tasks
                # pattern. The sweep only supersedes OTHER (older) lessons, never
                # the one just written (self-match scores ~1.0, above the 0.85
                # candidate ceiling), so deferring it is safe. No retry/queue: a
                # missed sweep self-heals on the next learn_add touching the topic.
                task = asyncio.create_task(
                    _resolve_and_supersede(state, sk, rule, candidates, vs)
                )
                state._background_tasks.add(task)
                task.add_done_callback(state._background_tasks.discard)
    else:
        lesson = Lesson(
            rule=rule,
            category=category,
            negative=negative,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        if scope == "workspace":
            ws = cleaned.get("workspace")
            _get_lessons(state, ws).save(lesson)
        else:
            state.lessons.save(lesson)
    state.push_refresh("lessons")
    return web.json_response({"ok": True})


async def api_lessons_delete(request: web.Request) -> web.Response:
    """DELETE /api/lessons — remove lessons by substring."""
    state: DashboardState = request.app["state"]
    # Block lesson deletes from temporary sessions only.
    # Incognito allows learn_remove (active user action).
    if _blocks_reads_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk,
            operation="lessons.delete",
            outcome="denied",
            source="dashboard",
            resources=sk,
        )
        return web.json_response(
            {"error": "Memory writes are not allowed in this session mode."}, status=403
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    rule_sub = body.get("rule", "").strip()
    if not rule_sub:
        return web.json_response({"error": "rule substring required"}, status=400)
    scope = body.get("scope", "global")
    # Delete from vector store if active, else JSONL
    vs = _get_memory(state).vector_store
    vs_lessons = vs.get_lessons() if vs else None
    if vs_lessons:
        ok = vs.delete_lesson(rule_sub)
    else:
        if scope == "workspace":
            ws = body.get("workspace")
            ok = _get_lessons(state, ws).remove(rule_sub)
        else:
            ok = state.lessons.remove(rule_sub)
    if ok:
        state.push_refresh("lessons")
    return web.json_response({"ok": ok})


async def api_crons(request: web.Request) -> web.Response:
    from kiro_crew.cron import compute_next_run_ts, format_schedule, get_local_tz  # noqa: F811

    state: DashboardState = request.app["state"]
    # Freshness-guaranteed list for the user-facing GET: offloads a locked
    # _sync() + snapshot to a worker thread so a cron just created by a separate
    # process (CLI / MCP) shows up immediately, without ever blocking the event
    # loop with the store read/hash. The hot per-connection status push and the
    # other mutation handlers keep using the cache-only list_jobs().
    jobs = await state.crons.list_jobs_async(include_disabled=True)
    now = time.time()
    tz_name, _ = get_local_tz()
    data = [
        {
            "id": j.id,
            "name": redact_credentials(redact_exfiltration_urls(j.name)[0])[0],
            "message": redact_credentials(redact_exfiltration_urls(j.message)[0])[0],
            "enabled": j.enabled,
            "schedule": redact_credentials(redact_exfiltration_urls(format_schedule(j.schedule, tz_name=j.timezone or tz_name))[0])[0],
            "cron_expr": j.schedule.cron_expr if j.schedule.kind == "cron" else None,
            "every_secs": j.schedule.every_secs if j.schedule.kind == "every" else None,
            "created_ts": j.created_ts or None,
            "last_status": j.last_status,
            "agent": redact_credentials(redact_exfiltration_urls(j.agent_id or "")[0])[0] or None,
            "model": redact_credentials(redact_exfiltration_urls(j.model or "")[0])[0] or None,
            "channel": redact_credentials(redact_exfiltration_urls(j.channel or "")[0])[0] or None,
            "approval_mode": redact_credentials(redact_exfiltration_urls(j.approval_mode or "")[0])[
                0
            ]
            or None,
            "silent": j.silent,
            "strict_schedule": j.strict_schedule,
            "hide_in_chat": j.hide_in_chat,
            "folder_id": j.folder_id,
            "last_run_ts": j.last_run_ts,
            "has_result": bool(j.last_result),
            "has_slot": state.has_slot(f"cron-{j.id}"),
            "next_run_ts": compute_next_run_ts(j, now=now),
            "timezone": redact_credentials(redact_exfiltration_urls(j.timezone or "")[0])[0]
            or None,
            "skip_dates": (
                [redact_credentials(redact_exfiltration_urls(d)[0])[0] for d in j.skip_dates]
                if j.skip_dates
                else None
            ),
            "script": redact_credentials(redact_exfiltration_urls(j.script or "")[0])[0] or None,
            "command": redact_credentials(redact_exfiltration_urls(j.command or "")[0])[0] or None,
            "last_result": redact_credentials(redact_exfiltration_urls(j.last_result or "")[0])[0] or None,
            "last_error": redact_credentials(redact_exfiltration_urls(j.last_error or "")[0])[0] or None,
            "is_running": state.crons.is_running(j.id),
            "running_since": state.crons.running_since(j.id),
        }
        for j in jobs
    ]
    return web.json_response(
        {
            "jobs": data,
            "server_tz": redact_credentials(redact_exfiltration_urls(tz_name or "")[0])[0] or None,
        }
    )


# ── Cron Folders ──

# Serializes all cron-folder mutations (create/rename/delete) so concurrent
# requests cannot race on the in-memory list + disk persist cycle. The lock is
# created lazily and re-created if the running event loop changes (Python 3.10
# binds a Lock to the loop it first waits on) — mirrors _get_config_lock in
# agents.py.
_cron_folders_lock: asyncio.Lock | None = None
_cron_folders_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_cron_folders_lock() -> asyncio.Lock:
    """Return a cron-folders lock bound to the current event loop."""
    global _cron_folders_lock, _cron_folders_lock_loop
    loop = asyncio.get_running_loop()
    if _cron_folders_lock is None or _cron_folders_lock_loop is not loop:
        _cron_folders_lock = asyncio.Lock()
        _cron_folders_lock_loop = loop
    return _cron_folders_lock


async def api_cron_folders(request: web.Request) -> web.Response:
    """GET /api/cron-folders — list all cron folders."""
    state: DashboardState = request.app["state"]
    # Bare list, matching the chat-folders precedent (api_chat_folders).
    return web.json_response(state._cron_folders)


async def api_cron_folders_create(request: web.Request) -> web.Response:
    """POST /api/cron-folders — create a new cron folder."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        return web.json_response(
            {"error": "name must be a string", "code": "name_required"}, status=400
        )
    name = body["name"].strip()
    if not name:
        return web.json_response({"error": "name is required", "code": "name_required"}, status=400)
    if len(name) > MAX_SHORT_STRING:
        return web.json_response({"error": "name too long", "code": "name_too_long"}, status=400)

    async with _get_cron_folders_lock():
        folder_id = uuid.uuid4().hex[:8]
        try:
            folder = await asyncio.to_thread(state.create_cron_folder, name, folder_id)
        except Exception:
            logger.warning("Failed to persist cron folder create", exc_info=True)
            return web.json_response(
                {"error": "failed to save folder", "code": "folder_save_failed"}, status=500
            )
    state.push_refresh("crons")
    return web.json_response(folder)


async def api_cron_folders_update(request: web.Request) -> web.Response:
    """PATCH /api/cron-folders/{folder_id} — rename a cron folder."""
    state: DashboardState = request.app["state"]
    folder_id = request.match_info["folder_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        return web.json_response(
            {"error": "name must be a string", "code": "name_required"}, status=400
        )
    name = body["name"].strip()
    if not name:
        return web.json_response({"error": "name is required", "code": "name_required"}, status=400)
    if len(name) > MAX_SHORT_STRING:
        return web.json_response({"error": "name too long", "code": "name_too_long"}, status=400)

    async with _get_cron_folders_lock():
        try:
            folder = await asyncio.to_thread(state.rename_cron_folder, folder_id, name)
        except Exception:
            logger.warning("Failed to persist cron folder rename", exc_info=True)
            return web.json_response(
                {"error": "failed to save folder", "code": "folder_save_failed"}, status=500
            )
    if folder is None:
        return web.json_response(
            {"error": "folder not found", "code": "folder_not_found"}, status=404
        )
    state.push_refresh("crons")
    return web.json_response(folder)


async def api_cron_folders_delete(request: web.Request) -> web.Response:
    """DELETE /api/cron-folders/{folder_id} — delete folder and clear assignments."""
    state: DashboardState = request.app["state"]
    folder_id = request.match_info["folder_id"]
    async with _get_cron_folders_lock():
        try:
            found = await asyncio.to_thread(state.delete_cron_folder, folder_id)
        except Exception:
            logger.warning("Failed to persist cron folder delete", exc_info=True)
            return web.json_response(
                {"error": "failed to save folder", "code": "folder_save_failed"}, status=500
            )
    if not found:
        return web.json_response(
            {"error": "folder not found", "code": "folder_not_found"}, status=404
        )
    state.push_refresh("crons")
    return web.json_response({"ok": True})


async def api_lessons(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    # Block lesson reads only for temporary sessions (blocks_reads=True).
    # Incognito sessions can read lessons (memory context is already injected).
    if _blocks_reads_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="lessons.list", outcome="denied",
            source="dashboard", resources=sk,
        )
        return web.json_response({"lessons": []})
    workspace = request.query.get("workspace")
    # Read from vector store if it has lessons, else JSONL
    vs = _get_memory(state).vector_store
    vs_lessons = vs.get_lessons() if vs else None
    if vs_lessons:
        data = []
        for e in vs_lessons[-50:]:
            try:
                rule = json.loads(e["value_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            data.append({"rule": rule, "category": "knowledge", "ts": e.get("updated_at", "")})
    else:
        # Merge global + workspace-scoped lessons
        global_lessons = state.lessons.load_all()
        ws = workspace or _get_active_workspace(state)
        if ws != "default":
            ws_lessons = _get_lessons(state, ws).load_all()
            seen = {le.rule.lower().strip() for le in global_lessons}
            for le in ws_lessons:
                if le.rule.lower().strip() not in seen:
                    global_lessons.append(le)
        data = [
            {"rule": le.rule, "category": le.category, "ts": le.ts} for le in global_lessons[-50:]
        ]
    return web.json_response({"lessons": data})

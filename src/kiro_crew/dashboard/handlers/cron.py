"""Cron job and Lessons CRUD API handlers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import (
    CronPendingMismatch,
    CronStoreBusy,
    CronStoreUnreadable,
    is_valid_timezone,
)
from kiro_crew.cron_script import (
    _read_script_body,
    bump_grant_epoch,
    commit_grant_epoch,
    compute_secret_env_pin,
    delivery_fingerprint,
    peek_grant_epoch,
    resolve_script_path,
    validate_secret_env_grant,
)
from kiro_crew.dashboard.cron_inject import (
    hydrate_slot_from_history,
    inject_cron_result_to_dashboard,
)
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState, SlotOrigin
from kiro_crew.executors import discovery_executor
from kiro_crew.history import is_incognito_transcript
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.secrets import SecretVault
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import (
    _MODEL_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    LEARN_ADD_SCHEMA,
    MAX_CRON_MESSAGE,
    MAX_SHORT_STRING,
    SLACK_THREAD_TS_RE,
    ValidationError,
    normalize_lesson_category,
    validate_string_field,
    validate_tool_args,
)

from ._shared import (
    _blocks_reads_session,
    _get_active_workspace,
    _get_lessons,
    _get_memory,
    _is_restricted_session,
    _probe_persisted_session,
    _redact_memory_field,
    read_bounded_json,
)

logger = logging.getLogger(__name__)

# 409 Conflict body returned when a cron-store mutator times out waiting for the
# store lock (CronStoreBusy). Contention is transient (a large atomic save on
# network storage, the CLI process, or the off-loop batch worker), so the client
# should retry rather than treat it as a hard failure. See CronService mutators.
_CRON_BUSY_STATUS = 409
_CRON_BUSY_BODY = {"error": "cron store busy, please retry", "retryable": True}

# Byte ceiling for a cron create/update body. The message field is bounded at
# MAX_CRON_MESSAGE characters, and one character costs at most 12 bytes on the
# wire: a client may send it JSON-escaped, and an astral character escapes to a
# \uXXXX\uXXXX surrogate pair (12 bytes), wider than raw UTF-8's 4-byte max. So
# this bounds the largest message the field validator will accept; the 64 KB of
# headroom covers the remaining short fields. Explicit rather than the shared
# default because a maximal multibyte message legitimately exceeds 64 KB.
_MAX_CRON_BODY_BYTES = 12 * MAX_CRON_MESSAGE + 64 * 1024

# Returned when the store cannot be WRITTEN because the last read of it failed
# (CronStoreUnreadable). 409 for the same reason as busy above -- the request
# conflicts with the current state of the resource -- but explicitly
# `retryable: False`: an unreadable file does not heal on its own, so a client
# that retries on busy must NOT retry on this. The exception already carries the
# one action that resolves it (move the file aside), so its message is surfaced
# verbatim rather than restated.
#
# The status and the code are written as LITERALS at the json_response call
# rather than hoisted into module constants, because `test_error_code_contract`
# buckets a computed `status=` as `dynamic_status` and caps that bucket
# deliberately -- a named constant is indistinguishable, to a static scan, from
# computing the status to evade the gate. Literals make this response decidable:
# it scores `compliant` instead of consuming cap.


def _audit_unavailable_response(decision: str) -> web.Response:
    """503 for a grant decision refused because its audit record could not be
    written. Every secret decision — approve, deny, revoke — is audit-or-deny:
    the SEL intent record is written synchronously (``critical=True``) BEFORE
    the store mutates, and an unwritable store refuses the decision with
    nothing changed. Privilege-reducing decisions take the same gate on
    purpose: a revocation nobody can later account for is exactly the kind of
    event the audit log exists to record, and the operator keeps unaudited
    kill switches (pausing the job, deleting the vault entry) for an outage.
    """
    return web.json_response(
        {
            "error": f"audit log unavailable — the {decision} was NOT applied; "
            "fix the audit store and decide again",
            "code": "audit_unavailable",
        },
        status=503,
    )


def _is_str_mapping(value: object) -> bool:
    """True when ``value`` is a dict whose keys AND values are all ``str``.

    The grant fields on a stored job are loaded verbatim from the
    agent-writable cron store, so the dataclass annotation is a promise the
    persisted bytes need not keep; every consumer that copies or iterates one
    checks the shape first.
    """
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def _redacted_grant_map(m: dict[str, str]) -> dict[str, str] | None:
    """Owner-view copy of a grant mapping with every key AND value scanned.

    The cron store is agent-writable and ``_job_from_record`` loads these
    dicts verbatim (the write-path grant grammar only covers
    product-mediated writes), so — like every sibling serialized field —
    nothing agent-authored reaches the dashboard without the
    credential/exfiltration redaction. The same verbatim load means the
    value may not even BE a dict (an agent can write a list or a string
    into the store field): anything non-dict serializes as None instead of
    crashing the owner's Schedule poll with a 500.
    """
    if not isinstance(m, dict) or not m:
        return None
    return {
        redact_credentials(redact_exfiltration_urls(str(k))[0])[0]: redact_credentials(
            redact_exfiltration_urls(str(v))[0]
        )[0]
        for k, v in m.items()
    }


def _cron_unreadable_response(exc: CronStoreUnreadable) -> web.Response:
    """Translate a refused write into a structured, non-retryable 409."""
    return web.json_response(
        {"error": str(exc), "code": "cron_store_unreadable", "retryable": False},
        status=409,
    )


def _invalid_path_id_response(value: str, name: str) -> web.Response | None:
    """Guard a URL path id (job_id/run_id/folder_id) for non-empty, bounded length.

    Returns a 400 ``invalid_<name>`` response when ``value`` is empty or longer
    than ``MAX_SHORT_STRING``, else ``None``. This is the single validator the
    cron routes apply to every path-param id — the job/run routes and both
    cron-folder routes — so a malformed id is rejected before any lock
    acquisition, thread dispatch, or state lookup (the asymmetric-perimeter gap
    #5789/#5808 closed). These ids are server-minted, so an over-long value only
    arrives from a malformed/hostile client.
    """
    if not value or len(value) > MAX_SHORT_STRING:
        return web.json_response(
            {"error": f"invalid {name} format", "code": f"invalid_{name}"}, status=400
        )
    return None


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
        prompt = _CONTRADICTION_PROMPT.format(old_rule=candidate["rule"], new_rule=new_rule)
        try:
            verdict = await _classify_contradiction(state, prompt)
        except Exception:
            logger.debug("Contradiction check failed for %r", candidate["key"], exc_info=True)
            continue
        if verdict == "CONTRADICTORY":
            logger.info(
                "Contradiction: new %r supersedes %r (sim=%.2f)",
                new_rule[:60],
                candidate["rule"][:60],
                candidate["similarity"],
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
                caller=sk,
                operation="lesson.contradiction_superseded",
                outcome="allowed",
                source="dashboard",
                resources=key,
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
    # Per-route cap: the body carries the job's full agent message/prompt text,
    # whose field bound (MAX_CRON_MESSAGE chars) can exceed the shared 64 KB
    # default in multibyte UTF-8 -- _MAX_CRON_BODY_BYTES sizes the ceiling to it.
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_CRON_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Type-validate every string field BEFORE calling string methods on it.
    # A JSON array/dict/int in these fields would otherwise raise AttributeError
    # (.strip() on a non-str) -> HTTP 500. validate_string_field enforces
    # isinstance(str), sanitizes, and bounds length, mirroring the MCP
    # CRON_ADD_SCHEMA so the REST + tool paths validate identically.
    try:
        name = validate_string_field(body, "name", required=True, max_len=MAX_SHORT_STRING)
        message = validate_string_field(body, "message", max_len=MAX_CRON_MESSAGE)
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
        except CronStoreUnreadable as exc:
            return _cron_unreadable_response(exc)
    elif cron_expr:
        try:
            job = await state.crons.add_job_async(name, message, cron_expr=cron_expr, **add_kwargs)
        except CronStoreBusy:
            return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
        except CronStoreUnreadable as exc:
            return _cron_unreadable_response(exc)
    else:
        return web.json_response({"error": "schedule, every, or cron required"}, status=400)
    state.push_refresh("crons")
    return web.json_response({"ok": True, "id": job.id})


async def api_cron_delete(request: web.Request) -> web.Response:
    """DELETE /api/crons/{id} — remove a cron job."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    try:
        ok = await state.crons.remove_job_async(job_id, actor="dashboard", source="api_cron_delete")
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
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
    # Default cap: the body is a bounded list of short job ids.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return web.json_response({"error": "ids must be a non-empty array"}, status=400)
    if not all(isinstance(i, str) for i in ids):
        return web.json_response({"error": "ids must be an array of strings"}, status=400)
    # De-duplicate while preserving order (a select-all + click race can send dupes).
    unique_ids = list(dict.fromkeys(ids))
    if len(unique_ids) > _MAX_BATCH_DELETE:
        return web.json_response({"error": f"too many ids (max {_MAX_BATCH_DELETE})"}, status=400)
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
        deleted, failed = await state.crons.remove_jobs(
            unique_ids, actor="dashboard", source="api_cron_batch_delete"
        )
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
                job_id,
                exc_info=True,
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
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    # Per-route cap: a partial update can carry the job's full agent
    # message/prompt text, whose field bound (MAX_CRON_MESSAGE chars) can
    # exceed the shared 64 KB default in multibyte UTF-8. The helper also owns
    # the non-object 400: a syntactically valid scalar or array parses fine
    # and then has no .get, so the field reads below would raise
    # AttributeError and surface as a 500.
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_CRON_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
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
    # name routes through the same validator as POST (type check +
    # sanitize_string + length cap) so the two REST surfaces cannot diverge:
    # PATCH previously passed it through entirely unvalidated, letting a
    # non-string or oversize name persist verbatim into crons.json.
    if "name" in kwargs:
        try:
            kwargs["name"] = validate_string_field(body, "name", max_len=MAX_SHORT_STRING)
        except ValidationError as exc:
            return web.json_response({"error": str(exc), "code": "invalid_name"}, status=400)
    # message routes through the same validator as POST (type check +
    # sanitize_string + length cap) so the two REST surfaces cannot diverge:
    # PATCH previously passed it through entirely unvalidated. Sanitizing here
    # also keeps length measured post-normalization, matching create.
    if "message" in kwargs:
        try:
            kwargs["message"] = validate_string_field(body, "message", max_len=MAX_CRON_MESSAGE)
        except ValidationError as exc:
            return web.json_response({"error": str(exc), "code": "invalid_message"}, status=400)
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
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    state.push_refresh("crons")
    return web.json_response({"ok": True, "id": job.id})


async def _promote_pending_grant(
    state: DashboardState,
    job_id: str,
    expected_source_sha256: str,
    expected_env: dict[str, str],
    expected_ts: float,
) -> web.Response:
    """Promote a job's PENDING secret request to the active grant.

    The single verified promotion path, reached only through the PUT
    ``approve_pending`` branch (approval lives exclusively on the dashboard
    Schedule page): re-validates the mapping, re-verifies
    the pending pin against the job's CURRENT code (409 ``code_changed`` on
    drift — an approval never blesses code that changed after the request),
    re-checks vault-name existence, then swaps pending -> active in one store
    update.

    ``expected_env``/``expected_ts`` bind the approval to the request the
    approver actually SAW: the agent can replace a pending request at any
    moment (cron_secret_request overwrites), so an approval that does not
    restate the displayed mapping could promote a different request than the
    one reviewed. Both are required and compared unconditionally — an
    optional check is a check the agent can arrange to skip — and a mismatch
    refuses with 409 ``stale_request`` instead of promoting.

    ``expected_source_sha256`` binds the approval to the script BODY the
    approver saw (the ``sha256`` GET ``/api/crons/{id}/script`` returned with
    the source it rendered). The request-level binding above is not enough on
    its own: an agent can rewrite the script and re-issue the request, and a
    dashboard whose request banner refreshed while its source view did not
    would then restate the NEW request's mapping and timestamp — passing both
    checks — while the operator is still looking at the OLD code. The pin only
    proves the code matches the request, never that the approver read it, so
    the approval must present the digest of what it displayed, and a digest
    that does not match the body this promotion pins is a 409 ``stale_source``.
    The field is required, not optional: an approval that omits it is an
    approval of code nobody attested to having seen. And a digest can only
    attest to what was SHOWN, so a body the dashboard cannot render verbatim
    (see ``_display_source``) is refused outright with 409
    ``source_not_reviewable`` — otherwise an agent could hide executable
    content behind a span the redactor masks and the operator would approve a
    raw digest over code the display never let them read.
    """
    job = await state.crons.get_job_async(job_id)
    if not job:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    # The store is agent-writable and ``_job_from_record`` loads both grant
    # fields verbatim, so either may be a list, a string or a dict with
    # non-string members. Refuse that shape here rather than letting the
    # ``dict(...)`` snapshots below raise into a 500: the owner sees a
    # structured refusal and the planted value never gets promoted.
    if not _is_str_mapping(job.secret_env) or not _is_str_mapping(job.secret_env_pending):
        return web.json_response(
            {
                "error": "the job's stored secret grant is malformed — revoke it and "
                "have the agent re-request",
                "code": "malformed_grant_state",
            },
            status=400,
        )
    if not job.secret_env_pending:
        return web.json_response(
            {"error": "no pending request", "code": "no_pending_request"}, status=404
        )
    if job.secret_env_pending != expected_env:
        return web.json_response(
            {
                "error": "the pending request changed after it was displayed — "
                "review the current request and approve again",
                "code": "stale_request",
            },
            status=409,
        )
    if job.secret_env_pending_ts != expected_ts:
        return web.json_response(
            {
                "error": "the pending request was re-issued after it was displayed — "
                "review the current request and approve again",
                "code": "stale_request",
            },
            status=409,
        )
    pending_snapshot = dict(job.secret_env_pending)
    pending_ts_snapshot = job.secret_env_pending_ts
    # Snapshot the PRIOR active grant too: the promoting write below replaces
    # it, and an epoch-commit failure must compensate by restoring it — its
    # pin was minted under the still-current epoch (the failed commit never
    # advanced it), so grant A stays exactly as valid as before this attempt.
    prior_secret_env = dict(job.secret_env)
    prior_secret_env_pin = job.secret_env_pin
    try:
        validate_secret_env_grant(pending_snapshot)

        # Verify the PENDING pin (what the request minted), then mint the
        # ACTIVE pin the runners honour — separate HMAC domains, so a pending
        # pin copied verbatim into the active fields never verifies. BOTH pins
        # derive from ONE script-body snapshot: a second read would let an
        # agent swap the file between them and get unseen code blessed. The
        # approver's source digest is checked against that SAME snapshot for
        # the same reason.
        def _both_pins() -> tuple[str, str, int, str, bool]:
            body_snapshot: bytes | None = None
            if job.script:
                file_path, _func = resolve_script_path(job.script)
                body_snapshot = _read_script_body(file_path)
            body_sha256 = hashlib.sha256(body_snapshot or b"").hexdigest()
            # Same verdict the source endpoint renders, re-derived from THIS
            # snapshot: the client's copy of the flag is not trusted.
            _shown, reviewable = _display_source(body_snapshot or b"")
            delivery = delivery_fingerprint(
                job.session_key, job.silent, job.channel or "", job.thread_ts or ""
            )
            pending_now = compute_secret_env_pin(
                job.script,
                job.command,
                job.message,
                job_id=job.id,
                grant=pending_snapshot,
                domain="pending",
                body=body_snapshot,
                delivery=delivery,
            )
            # Mint under the NEXT epoch without writing it: the epoch commits
            # only after the store swap succeeds, so a refused approval (code
            # drift, stale request, store busy) never invalidates an existing
            # active grant this operation did not replace.
            next_epoch = peek_grant_epoch(job.id)
            active = compute_secret_env_pin(
                job.script,
                job.command,
                job.message,
                job_id=job.id,
                grant=pending_snapshot,
                domain="active",
                body=body_snapshot,
                epoch=next_epoch,
                delivery=delivery,
            )
            return pending_now, active, next_epoch, body_sha256, reviewable

        pending_pin_now, active_pin, next_epoch, body_sha256, reviewable = await asyncio.to_thread(
            _both_pins
        )
    except (ValueError, FileNotFoundError, PermissionError, RuntimeError) as exc:
        return web.json_response({"error": str(exc), "code": "invalid_secret_env"}, status=400)
    if pending_pin_now != job.secret_env_pending_pin:
        return web.json_response(
            {
                "error": "the job's code changed after this request was made — "
                "review the current script/command, then ask the agent to "
                "re-request (or grant directly)",
                "code": "code_changed",
            },
            status=409,
        )
    if not reviewable:
        # The dashboard cannot show this body faithfully (redaction masked a
        # span, or it is not valid UTF-8), so no digest the operator echoes
        # can attest to having read the code that would run. Not approvable.
        return web.json_response(
            {
                "error": "parts of this script cannot be shown as written (masked "
                "credential-like text or undecodable bytes), so what the reviewer "
                "sees is not exactly what would run — rewrite the script so its "
                "source displays verbatim, then ask the agent to re-request",
                "code": "source_not_reviewable",
            },
            status=409,
        )
    if body_sha256 != expected_source_sha256:
        return web.json_response(
            {
                "error": "the script shown is not the script this request would run — "
                "reload the job, review the current source, and approve again",
                "code": "stale_source",
            },
            status=409,
        )
    known = set(await asyncio.to_thread(SecretVault(config_dir()).list_names))
    missing = sorted(set(pending_snapshot.values()) - known)
    if missing:
        # The names are agent-authored (they came in via the pending request),
        # so they take the same credential/exfiltration scrub as every other
        # agent-written string the dashboard echoes.
        shown = [redact_credentials(redact_exfiltration_urls(str(n))[0])[0] for n in missing]
        return web.json_response(
            {
                "error": "unknown vault secret name(s): " + ", ".join(shown),
                "code": "unknown_secret",
            },
            status=400,
        )
    # AUDIT-OR-DENY: a secret grant must never exist unaudited. The intent
    # record is written (and awaited, off-loop) BEFORE the promoting write;
    # an unwritable SEL store refuses the approval with nothing mutated —
    # the same fail-closed posture the repo applies to other privileged
    # mutations. ``critical=True`` is what makes that true: the default SEL
    # path enqueues for a background writer and swallows a filesystem
    # failure, so only a critical write surfaces it here. The terminal
    # success event below stays best-effort: an applied grant is already
    # covered by this record. Names only — env keys and vault names, never
    # values. _sel() is resolved INSIDE the worker lambda: a fresh gateway's
    # first call initializes the SEL store (trust key + log files), which
    # must never run on the event loop.
    try:
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller="dashboard",
                operation="cron.secret_request_approved",
                outcome="invoked",
                source="dashboard",
                resources=f"{job_id}:{','.join(sorted(pending_snapshot))}",
                critical=True,
            )
        )
    except Exception:
        logger.warning("SEL unavailable; refusing grant approval for %s", job_id, exc_info=True)
        return _audit_unavailable_response("approval")
    try:
        updated = await state.crons.update_job_async(
            job_id,
            secret_env=pending_snapshot,
            secret_env_pin=active_pin,
            # The pending request is CONSUMED in the same atomic write that
            # promotes it. Leaving it behind would let a concurrent decision
            # pass its own compare-and-swap against the same snapshot: a deny
            # would report success while this grant stays active, and a second
            # approval would re-mint over it. A commit failure below RESTORES
            # the request (compensating write), so the re-approve path
            # survives without that window.
            secret_env_pending={},
            # Locked compare-and-swap: everything above ran against a snapshot
            # the agent could have replaced in the meantime; the store refuses
            # the swap unless the record STILL carries exactly that snapshot.
            expect_secret_env_pending=pending_snapshot,
            expect_secret_env_pending_ts=pending_ts_snapshot,
        )
    except CronPendingMismatch:
        return web.json_response(
            {
                "error": "the pending request changed after it was displayed — "
                "review the current request and approve again",
                "code": "stale_request",
            },
            status=409,
        )
    except CronStoreBusy:
        return web.json_response(
            {
                "error": "cron store busy, please retry",
                "retryable": True,
                "code": "cron_store_busy",
            },
            status=409,
        )
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    except ValueError as e:
        return web.json_response({"error": str(e), "code": "invalid_secret_env"}, status=400)
    if not updated:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    # The swap landed: commit the epoch the pin was minted under. A crash in
    # this gap leaves the NEW grant failing closed (re-approve heals), never
    # a dead pin on a grant this operation did not replace. The commit is
    # compare-and-swap on the epoch this mint peeked: a concurrent revoke or
    # job removal that bumped meanwhile REFUSES the commit — re-committing
    # the bumped value would re-validate the very pin that bump meant to
    # kill. On refusal, bump once more so the just-swapped pin is dead too
    # (fail closed), then surface the conflict for a fresh approval.
    try:
        committed = await asyncio.to_thread(
            commit_grant_epoch, job.id, next_epoch, expected_current=next_epoch - 1
        )
    except (OSError, ValueError):
        # The swap landed but the epoch could not be committed: the stored pin
        # was minted under an uncommitted epoch, so every run refuses it (fail
        # closed). The request was consumed by the promoting write above, so
        # RESTORE it here — unless the agent already posted a NEWER request
        # into the gap, which stays (a fresh ask awaiting its own approval,
        # never silently overwritten by the old one's restoration).
        logger.warning("Grant-epoch commit failed for job %s", job.id, exc_info=True)

        async def _compensate(**kwargs: Any) -> bool:
            # CronStoreBusy is transient lock contention — the promoting
            # write succeeded moments ago, so a short bounded retry recovers
            # almost every real case instead of abandoning grant A to the
            # dead just-swapped pin. Unreadable state and invalid input
            # cannot heal on retry and fall through to the loud path below.
            for attempt in range(3):
                try:
                    await state.crons.update_job_async(job_id, **kwargs)
                    return True
                except CronStoreBusy:
                    await asyncio.sleep(0.2 * (attempt + 1))
                except (CronStoreUnreadable, ValueError):
                    break
            return False

        restored = False
        try:
            restored = await _compensate(
                # FULL compensation: the promoting write replaced active grant
                # A with a pin minted under the uncommitted epoch (dead), so
                # restore A alongside the consumed request — A's pin is still
                # valid, the failed commit never advanced the epoch.
                secret_env=prior_secret_env,
                secret_env_pin=prior_secret_env_pin,
                secret_env_pending=pending_snapshot,
                secret_env_pending_pin=pending_pin_now,
                secret_env_pending_ts=pending_ts_snapshot,
                expect_secret_env_pending={},
                expect_secret_env_pending_ts=0.0,
                # The ACTIVE fields must still hold the just-promoted (dead)
                # grant: a concurrent revoke that already cleared them owns
                # the state now, and restoring A over the operator's
                # revocation would resurrect what they withdrew.
                expect_secret_env=pending_snapshot,
                expect_secret_env_pin=active_pin,
            )
        except CronPendingMismatch as mismatch:
            if "active grant" in str(mismatch):
                # A concurrent writer (revoke) replaced the active fields in
                # the gap: their decision stands. Nothing to restore — the
                # dead just-swapped pin is already gone.
                logger.info("Compensation on %s skipped (grant changed concurrently)", job_id)
                restored = True
            else:
                # A NEWER request landed in the gap: leave it (never
                # overwritten by the old one's restoration), but STILL
                # restore the prior active grant in its own write — the dead
                # just-swapped pin must not stand in for grant A regardless
                # of the pending slot. Same active-field guard applies.
                logger.info("Pending restore on %s skipped (newer request)", job_id)
                try:
                    restored = await _compensate(
                        secret_env=prior_secret_env,
                        secret_env_pin=prior_secret_env_pin,
                        expect_secret_env=pending_snapshot,
                        expect_secret_env_pin=active_pin,
                    )
                except CronPendingMismatch:
                    logger.info("Compensation on %s skipped (grant changed concurrently)", job_id)
                    restored = True
        if not restored:
            # Double fault: the store accepted the promoting write but then
            # refused every compensation attempt. The dead just-swapped pin
            # stays persisted (runs refuse it — still fail-closed), but the
            # prior grant is NOT restored; say so loudly instead of
            # pretending the compensation landed.
            logger.critical(
                "Compensation failed for %s: prior grant not restored after "
                "epoch-commit failure",
                job_id,
            )
            return web.json_response(
                {
                    "error": "the grant's revocation epoch could not be committed "
                    "AND the compensating restore failed; the stored grant is "
                    "inactive (runs refuse its pin) — fix the cron/epoch storage, "
                    "then re-approve",
                    "code": "epoch_commit_failed",
                },
                status=503,
            )
        return web.json_response(
            {
                "error": "the grant's revocation epoch could not be committed; "
                "the previous grant was restored and the request is still "
                "pending — fix the epoch storage and approve again",
                "code": "epoch_commit_failed",
            },
            status=503,
        )
    if not committed:
        try:
            await asyncio.to_thread(bump_grant_epoch, job.id)
        except (OSError, ValueError):
            # Bump refused (corrupt state): every granted run already
            # refuses under it, so the direction is still closed.
            logger.warning("Conflict-bump failed for job %s", job.id, exc_info=True)
        return web.json_response(
            {
                "error": "the grant changed concurrently (a revoke or job removal "
                "raced this approval); ask the agent to re-request",
                "code": "grant_conflict",
            },
            status=409,
        )
    # The commit landed and the promoting write already consumed the pending
    # request, so the grant is fully active with nothing left to clear.
    try:
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller="dashboard",
                operation="cron.secret_request_approved",
                outcome="allowed",
                source="dashboard",
                resources=f"{job_id}:{','.join(sorted(updated.secret_env))}",
            )
        )
    except Exception:
        logger.debug("SEL logging failed for cron secret approve", exc_info=True)
    state.push_refresh("crons")
    return web.json_response(
        {"ok": True, "id": updated.id, "secret_env": _redacted_grant_map(updated.secret_env)}
    )


async def api_cron_secret_grant(request: web.Request) -> web.Response:
    """PUT /api/crons/{id}/secrets — revoke a grant or decide a pending request.

    Operator surface ONLY, enforced IN the handler: ``/api/crons`` is a PREFIX
    entry in the mixed internal paths (the CLI cron trigger needs it), so this
    route IS reachable with ``X-Internal-Secret`` — the credential every cron
    script subprocess and MCP process holds. Granting is the one cron mutation
    that must be human-only, so a proven internal-secret caller
    (``request["internal_auth"] is True``) is refused outright; only a
    cookie/token-authenticated browser caller proceeds. Body:
    ``{"secret_env": {}}`` (an EMPTY map) revokes — a non-empty map is
    refused, since request->approve is the only mint path —
    ``{"approve_pending": true}`` / ``{"deny_pending": true}`` act on an
    agent-requested pending grant. The code pin is computed HERE from the
    job's current script body — a client-supplied pin is
    ignored, so a grant always binds to the code the operator could inspect
    at grant time.
    """
    state: DashboardState = request.app["state"]
    # internal_auth is set solely after a constant-time X-Internal-Secret
    # match in token_auth_middleware — the machine credential. Machines
    # request (cron_secret_request); only humans grant. The denial is
    # SEL-audited like every other refused privileged operation: a machine
    # probing the grant endpoint is exactly the signal the audit log exists
    # to record.
    if request.get("internal_auth") is True:
        try:
            await asyncio.to_thread(
                lambda: _sel().log_api_access(
                    caller=str(request.get("user") or "internal"),
                    operation="cron.secret_grant",
                    outcome="denied",
                    source="dashboard",
                    resources=request.match_info.get("job_id", ""),
                    error="operator_only",
                )
            )
        except Exception:  # pragma: no cover - audit must never change the outcome
            logger.debug("SEL audit for machine secret-grant denial failed", exc_info=True)
        return web.json_response(
            {
                "error": "secret grants require the dashboard (operator) credential",
                "code": "operator_only",
            },
            status=403,
        )
    # And not just any human: a dashboard token is also minted for every
    # allowed Slack user (!dashboard), who is not the vault's owner. Granting
    # hands agent-authored code a vault value, so it is owner-only — the same
    # boundary ask_question's card resolution draws. The shared gate audits
    # the denial to SEL and reuses the one definition of "owner" (exact
    # owner_id match, or the signed local bootstrap subject when no owner is
    # configured).
    denied = await require_owner_dashboard_request(request, "cron.secret_grant")
    if denied is not None:
        return denied
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"}, status=400
        )
    job = await state.crons.get_job_async(job_id)
    if not job:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    # ── Agent-requested pending grants: approve / deny ──
    # The MCP cron_secret_request tool records a pending mapping + a pin of the
    # code at request time. Approval re-verifies that pin against the job's
    # CURRENT code so the operator only ever blesses what they could inspect —
    # a body changed after the request refuses with 409 rather than promoting.
    if body.get("deny_pending") is True:
        if not job.secret_env_pending:
            return web.json_response(
                {"error": "no pending request", "code": "no_pending_request"}, status=404
            )
        deny_expected = body.get("expected_secret_env")
        deny_expected_ts = body.get("expected_ts")
        # AUDIT-OR-DENY, same as approval: the intent record lands before the
        # request is discarded, synchronously, or the denial is refused.
        try:
            await asyncio.to_thread(
                lambda: _sel().log_api_access(
                    caller="dashboard",
                    operation="cron.secret_request_denied",
                    outcome="invoked",
                    source="dashboard",
                    resources=job_id,
                    critical=True,
                )
            )
        except Exception:
            logger.warning("SEL unavailable; refusing grant denial for %s", job_id, exc_info=True)
            return _audit_unavailable_response("denial")
        try:
            updated = await state.crons.update_job_async(
                job_id,
                secret_env_pending={},
                # A denial is a decision about the DISPLAYED request too: an
                # agent replacing it in the meantime must not have its unseen
                # request silently discarded by a stale click. The timestamp
                # additionally distinguishes a REISSUED request with an
                # identical mapping from the displayed one.
                expect_secret_env_pending=(
                    deny_expected if isinstance(deny_expected, dict) else None
                ),
                expect_secret_env_pending_ts=(
                    float(deny_expected_ts) if isinstance(deny_expected_ts, (int, float)) else None
                ),
            )
        except CronPendingMismatch:
            return web.json_response(
                {
                    "error": "the pending request changed after it was displayed — "
                    "review the current request and decide again",
                    "code": "stale_request",
                },
                status=409,
            )
        except CronStoreBusy:
            return web.json_response(
                {
                    "error": "cron store busy, please retry",
                    "retryable": True,
                    "code": "cron_store_busy",
                },
                status=409,
            )
        except CronStoreUnreadable as exc:
            return _cron_unreadable_response(exc)
        state.push_refresh("crons")
        return web.json_response({"ok": True, "id": job_id})
    if body.get("approve_pending") is True:
        # The approval must restate the request the approver saw (409
        # stale_request on drift) — see _promote_pending_grant. All three
        # snapshot fields are REQUIRED, not optional: an approval that omits
        # one attests to nothing about that dimension, so an agent could
        # re-issue the request between display and click and have the unseen
        # version promoted. The UI always sends the mapping it rendered, the
        # request timestamp and the source digest.
        expected_env = body.get("expected_secret_env")
        if not (
            isinstance(expected_env, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in expected_env.items())
        ):
            return web.json_response(
                {
                    "error": "expected_secret_env is required: the object mapping "
                    "env-var names to vault secret names exactly as displayed "
                    "for this approval",
                    "code": "invalid_secret_env",
                },
                status=400,
            )
        expected_ts = body.get("expected_ts")
        if isinstance(expected_ts, bool) or not isinstance(expected_ts, (int, float)):
            return web.json_response(
                {
                    "error": "expected_ts is required: the pending request timestamp "
                    "as displayed for this approval",
                    "code": "invalid_secret_env",
                },
                status=400,
            )
        # REQUIRED: the digest of the script source the approver reviewed
        # (``sha256`` from GET /api/crons/{id}/script). Without it the approval
        # attests to nothing about the code — see _promote_pending_grant.
        expected_source = body.get("expected_source_sha256")
        if not isinstance(expected_source, str) or not _SHA256_HEX_RE.fullmatch(expected_source):
            return web.json_response(
                {
                    "error": "expected_source_sha256 is required: the sha256 of the "
                    "script source reviewed for this approval, as returned by "
                    "GET /api/crons/{id}/script",
                    "code": "invalid_secret_env",
                },
                status=400,
            )
        return await _promote_pending_grant(
            state,
            job_id,
            expected_source_sha256=expected_source,
            expected_env=expected_env,
            expected_ts=float(expected_ts),
        )
    secret_env = body.get("secret_env")
    if not isinstance(secret_env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in secret_env.items()
    ):
        return web.json_response(
            {
                "error": "secret_env must be an object mapping env-var names "
                "to vault secret names",
                "code": "invalid_secret_env",
            },
            status=400,
        )
    pin = ""
    if secret_env:
        # Direct grants are removed BY DESIGN: nothing binds what the owner
        # saw to what gets pinned — the agent can rewrite the script between
        # the page render and the grant click, and the pin would bless the
        # rewrite. The request->approve flow is the only mint path: the
        # request pins the code at request time and approval refuses on
        # drift, so what the operator blesses is what the agent showed them.
        return web.json_response(
            {
                "error": "direct grants are not supported — have the agent "
                "call cron_secret_request, then approve the pending request "
                "here (the approval verifies the code has not changed since "
                "the request)",
                "code": "direct_grant_removed",
            },
            status=400,
        )
    # AUDIT-OR-DENY before anything moves — the epoch bump below is itself a
    # mutation (it kills the current pin), so the record must precede it.
    # Names only — env keys and vault names, never values.
    try:
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller="dashboard",
                operation="cron.secret_grant" if secret_env else "cron.secret_revoke",
                outcome="invoked",
                source="dashboard",
                resources=f"{job_id}:{','.join(sorted(secret_env)) or '-'}",
                critical=True,
            )
        )
    except Exception:
        logger.warning("SEL unavailable; refusing grant revocation for %s", job_id, exc_info=True)
        return _audit_unavailable_response("revocation")
    # Revoke (empty mapping): bump the epoch FIRST so a saved copy of the old
    # mapping+pin written back into the agent-writable store never verifies
    # again, even if the clear below fails.
    try:
        await asyncio.to_thread(bump_grant_epoch, job_id)
    except (OSError, ValueError):
        # Corrupt or unwritable epoch state: the grant is left in place —
        # every granted run already refuses under unreadable epoch state
        # (fail closed), and clearing the store WITHOUT the bump would let a
        # saved copy of the mapping+pin verify again once the state heals.
        logger.warning("Grant-epoch bump failed for revoke of %s", job_id, exc_info=True)
        return web.json_response(
            {
                "error": "the grant's revocation epoch could not be advanced; "
                "the grant was NOT cleared (runs refuse its pin while the "
                "epoch state is unhealthy) — fix the epoch storage and "
                "revoke again",
                "code": "epoch_bump_failed",
            },
            status=503,
        )
    try:
        updated = await state.crons.update_job_async(
            job_id, secret_env=secret_env, secret_env_pin=pin
        )
    except CronStoreBusy:
        return web.json_response(
            {
                "error": "cron store busy, please retry",
                "retryable": True,
                "code": "cron_store_busy",
            },
            status=409,
        )
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    except ValueError as e:
        return web.json_response({"error": str(e), "code": "invalid_secret_env"}, status=400)
    if not updated:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    state.push_refresh("crons")
    return web.json_response(
        {"ok": True, "id": updated.id, "secret_env": _redacted_grant_map(updated.secret_env)}
    )


async def api_cron_run(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/run — trigger immediate execution."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
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
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
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
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    slot_name = f"cron-{job_id}"
    jobs = state.crons.list_jobs(include_disabled=True)
    job = next((j for j in jobs if j.id == job_id), None)
    if job:
        history = (
            await asyncio.to_thread(state.conversation_log.read_messages, f"cron:{job.id}")
            if state.conversation_log
            else []
        )
        # Re-surfacing a stored result, not delivering a fresh run: the prompt
        # that produced it is not recoverable from live config -- see
        # inject_cron_result_to_dashboard's ``include_prompt``.
        inject_cron_result_to_dashboard(
            state, job, job.last_result or "", history=history, include_prompt=False
        )
    else:
        # Job deleted (one-shot with delete_after_run). Create slot from history or notification.
        session_key = f"cron:{job_id}"
        history = (
            await asyncio.to_thread(state.conversation_log.read_messages, session_key)
            if state.conversation_log
            else []
        )
        if history:
            slot = state.get_or_create_slot(name=slot_name, agent="", origin=SlotOrigin.CRON)
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
            slot = state.get_or_create_slot(name=slot_name, agent="", origin=SlotOrigin.CRON)
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
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    # Default cap: the body is a single flag. allow_absent keeps the
    # missing-body-means-defaults contract; a body that is PRESENT but
    # malformed is a 400; only an absent body defaults.
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    enabled = body.get("enabled", True)
    try:
        ok = await state.crons.enable_job_async(job_id, enabled=enabled)
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    if ok:
        state.push_refresh("crons")
    return web.json_response({"ok": ok})


async def api_cron_ack(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/ack — acknowledge a cron notification."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    # Default cap: the body is a short summary + notification ts. allow_absent
    # keeps the missing-body-means-defaults contract; see api_cron_enable.
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    summary = body.get("summary", "acknowledged")
    notification_ts = body.get("ts", "")
    try:
        ok = await state.crons.ack_job_async(job_id, summary)
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    if notification_ts:
        await state.ack_notification(notification_ts)
    return web.json_response({"ok": ok})


async def api_cron_history(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/history — paginated execution history (no trace)."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    try:
        limit = int(request.query.get("limit", "20"))
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        offset = 0
    runs, total = await state.crons.get_history().get_job_history(
        job_id, limit=limit, offset=offset
    )
    for run in runs:
        for key in ("summary", "error"):
            if run.get(key):
                run[key] = redact_credentials(redact_exfiltration_urls(run[key])[0])[0]
    return web.json_response({"runs": runs, "total": total})


async def api_cron_history_detail(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/history/{run_id} — full run detail with trace."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    run_id = request.match_info["run_id"]
    if (_e := _invalid_path_id_response(run_id, "run_id")) is not None:
        return _e
    detail = await state.crons.get_history().get_run_detail(job_id, run_id)
    if not detail:
        return web.json_response({"error": "run not found"}, status=404)
    for key in ("summary", "trace", "error"):
        if detail.get(key):
            detail[key] = redact_credentials(redact_exfiltration_urls(detail[key])[0])[0]
    return web.json_response(detail)


# Ceiling on the script source returned by GET /api/crons/{id}/script. Cron
# scripts are hand- or LLM-authored helpers of a few KB; anything near this
# ceiling is not a cron script, so the view truncates rather than streaming an
# unbounded file into the dashboard.
_SCRIPT_SOURCE_MAX_BYTES = 256 * 1024

# Shape of the ``sha256`` GET /api/crons/{id}/script returns and the approval
# echoes back as ``expected_source_sha256``: 64 lowercase hex digits, nothing
# else. Anything not in this shape is a malformed approval, refused before it
# reaches the promotion path.
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")

# The read below traverses the O_NOFOLLOW + fd-real-path chokepoint in hooks
# (safe_read_file_bytes_nolink), which has no Windows implementation
# (pinned_fs.fd_real_path returns None there -> fail-closed on every read). Gate with an
# honest 501 rather than an opaque refusal, mirroring the theme-pack routes.
_SCRIPT_SOURCE_WIN_UNSUPPORTED = os.name == "nt"


def _read_script_source_sync(
    script_spec: object,
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """Resolve a job's stored ``script`` spec and read its source (blocking).

    Returns ``(payload, None)`` on success or ``(None, (message, code))`` on
    refusal. Runs in a worker thread — resolution stats the filesystem and the
    read is synchronous file IO, neither of which may run on the event loop.

    The path is derived exclusively from the job's own stored ``script`` field
    (never from the client), re-validated by ``resolve_script_path`` (existence,
    sensitivity, containment under ``<config_dir>/crons/``), and then read
    through ``safe_read_file_bytes_nolink`` pinned to that same root so a
    symlink or hardlink swapped in after the by-name check is rejected, never
    dereferenced.

    INVARIANT: no persisted job state may produce a 500 from this endpoint.
    ``script_spec`` comes from ``crons.json``, which is agent- and hand-editable
    JSON — its value can be any JSON type and any string shape. Every failure
    to resolve it, of any kind, is therefore a 4xx refusal, never a crash:
    the spec is validated as a string up front, and the resolution step is
    wrapped fail-closed (``FileNotFoundError`` stays distinct only to give the
    honest 404).
    """
    if not isinstance(script_spec, str):
        # Truthy non-string ``script`` in crons.json (number, list, object):
        # the handler's ``if not job.script`` gate passes it through, and the
        # resolver would crash on it. Refuse, same code as any bad path.
        return None, ("script path refused", "script_path_refused")
    try:
        file_path, func_name = resolve_script_path(script_spec)
    except FileNotFoundError:
        return None, ("script file not found", "script_not_found")
    except Exception:
        # Fail-closed catch-all, deliberate: malformed spec (ValueError), a
        # path escaping the crons root (PermissionError), symlink-loop
        # resolution failures (RuntimeError on some Python versions,
        # OSError/ELOOP on others), and any failure mode not yet enumerated —
        # the spec is untrusted persisted data, so an unanticipated exception
        # type must degrade to the same refusal as an anticipated one, never
        # to a 500. The refusal does not echo resolution detail (the spec
        # string is already visible on the job record; the resolved path is
        # not the client's business).
        return None, ("script path refused", "script_path_refused")
    crons_root = str((config_dir() / "crons").resolve())
    truncated = False
    try:
        data = safe_read_file_bytes_nolink(
            file_path, within_root=crons_root, max_bytes=_SCRIPT_SOURCE_MAX_BYTES
        )
    except FileTooLargeError:
        data = safe_read_file_bytes_nolink(
            file_path,
            within_root=crons_root,
            max_bytes=_SCRIPT_SOURCE_MAX_BYTES,
            allow_truncate=True,
        )
        truncated = True
    if data is None:
        # Fail-closed refusal from the chokepoint (swapped symlink, hardlink,
        # non-regular file, unverifiable containment). 4xx, never a 500.
        return None, ("script unreadable", "script_read_refused")
    # Scripts under crons/ are LLM-writeable by design, so treat their content
    # like any other agent-influenced text shown in the dashboard: strip raw
    # credential patterns and exfiltration URLs before it leaves the backend.
    # The file and function names come from the same stored spec, so they get
    # the identical treatment — a credential-shaped name must not ride out on
    # the metadata fields either.
    source, reviewable = _display_source(data)
    file_name = redact_credentials(redact_exfiltration_urls(os.path.basename(file_path))[0])[0]
    func = redact_credentials(redact_exfiltration_urls(func_name)[0])[0]
    # Digest of the RAW bytes read (pre-redaction): the approval path hashes
    # the same raw body when it pins a grant, and the operator's approve call
    # echoes this value back so the server can prove the code it blesses is
    # the code this response rendered. Over a truncated read the digest covers
    # only the prefix and can never match a full-body pin — correct, since a
    # script the operator could not see in full is not approvable either.
    return {
        "source": source,
        "file": file_name,
        "function": func,
        "truncated": truncated,
        "reviewable": reviewable and not truncated,
        "sha256": hashlib.sha256(data).hexdigest(),
    }, None


def _display_source(data: bytes) -> tuple[str, bool]:
    """Render a script body for the dashboard and say whether the rendering IS
    the code.

    Returns ``(display_text, reviewable)``. The display text is the UTF-8
    decode with credential patterns and exfiltration URLs redacted. It is
    ``reviewable`` only when that text equals the raw decode byte-for-byte:
    the approval flow binds the operator's decision to the digest of the RAW
    bytes, so any byte the display does not show faithfully — a span the
    redactor masked, or an undecodable sequence the decoder replaced — is code
    the operator would be blessing without having seen it. An agent could
    hide executable content behind exactly such a span (a string the redactor
    matches, an invalid UTF-8 run inside a bytes literal), so a body whose
    display differs from its raw form is not approvable at all, and the
    promotion path re-derives this verdict from its own body snapshot rather
    than trusting the client's copy of the flag.
    """
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        lossy = data.decode("utf-8", errors="replace")
        return redact_credentials(redact_exfiltration_urls(lossy)[0])[0], False
    shown = redact_credentials(redact_exfiltration_urls(decoded)[0])[0]
    return shown, shown == decoded


async def api_cron_script_source(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/script — read-only source of a script cron's callable.

    The job id is the only caller-supplied input; the file path is derived
    server-side from the stored job record (see ``_read_script_source_sync``).
    """
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    # Freshness-guaranteed lookup, same rationale as api_cron_run: the job may
    # have been minted by another process and not yet be in the cache snapshot.
    job = await state.crons.get_job_async(job_id)
    if not job:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    if not job.script:
        return web.json_response({"error": "job has no script", "code": "no_script"}, status=404)
    if _SCRIPT_SOURCE_WIN_UNSUPPORTED:
        return web.json_response(
            {
                "error": "script source view is not yet supported on Windows",
                "code": "unsupported_platform",
            },
            status=501,
        )
    payload, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _read_script_source_sync, job.script
    )
    if err is not None:
        message, code = err
        # SEL audit: a refused read of an on-disk script is a guarded-path
        # permission decision (containment escape, symlink swap, unresolvable
        # spec) and must leave an audit record, same as an allowed read below.
        _sel().log_api_access(
            caller="dashboard",
            operation="cron.script_source",
            outcome="denied",
            source="api_cron_script_source",
            resources=f"job_id={job_id} code={code}",
        )
        # Literal statuses per branch (not a computed ``status=`` expression) so
        # the error-code contract ratchet can see each site is coded.
        if code == "script_not_found":
            return web.json_response({"error": message, "code": code}, status=404)
        return web.json_response({"error": message, "code": code}, status=422)
    # _read_script_source_sync returns exactly one of (payload, err) non-None.
    assert payload is not None
    _sel().log_api_access(
        caller="dashboard",
        operation="cron.script_source",
        outcome="ok",
        source="api_cron_script_source",
        resources=f"job_id={job_id} truncated={payload['truncated']}",
    )
    return web.json_response(payload)


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


# Delete-route policy for the archived-session recovery path: only a temporary
# session is blocked from deleting; incognito may delete (an active user
# action), mirroring the live-slot ``_blocks_reads_session`` policy. The create
# route blocks every private mode via the canonical
# ``history.is_incognito_transcript`` classifier instead.
def _is_temporary_transcript(persisted_mode: str) -> bool:
    return persisted_mode == "temporary"


async def _recognize_session(
    state: DashboardState,
    sk: str,
    operation: str,
    *,
    blocks_persisted_mode: Callable[[str], bool],
) -> web.Response | None:
    """Session-recognition gate shared by the lessons and memory routes.

    Applies one slot / restricted-key / channel-namespace / persisted-JSONL
    cascade to every caller so the mutating routes cannot diverge.
    Returns a refusal :class:`web.Response`, or ``None`` when the session is
    recognised. Every decision — allow or deny — emits a SEL audit event
    under *operation*.

    ``blocks_persisted_mode`` is the per-route policy for the
    archived-session recovery path: writes block every private mode (the
    canonical ``history.is_incognito_transcript`` classifier); lesson delete
    blocks only ``temporary``. A ``None``
    (unreadable or ambiguous) persisted mode always fails closed regardless
    of policy. Every refusal body carries a machine-readable ``code`` field
    (``missing_session_key`` / ``unknown_session`` on the 400s,
    ``restricted_session`` on the 403), so clients dispatch on the
    identifier rather than the prose.
    """
    if not sk:
        _sel().log_api_access(
            caller="anonymous",
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="missing_session_key",
        )
        return web.json_response(
            {"error": "missing X-Session-Key", "code": "missing_session_key"},
            status=400,
        )
    if sk == "dashboard:ui":
        # Browser UI's static key — implicitly trusted, but the allow
        # decision itself is still an authorization outcome and must be
        # audited (every permission decision emits a SEL event).
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="dashboard_ui",
        )
        return None
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
    #   * the first memory call in a fresh channel thread races the JSONL
    #     flush (which only lands after the LLM turn completes), so a
    #     namespace fast-path avoids a spurious HTTP 400 until the
    #     transcript is on disk; and
    #   * the ``_probe_persisted_session`` fallback below cannot
    #     rescue a channel key anyway — ``slot_name`` is
    #     ``sk.split(":", 1)[-1]`` (inner colons kept, channel prefix
    #     dropped) while the file is ``dashboard_<safe_key>.jsonl`` with
    #     colons folded to ``_``, so no probed name ever matches (and a
    #     colon is now rejected outright by ``_persisted_session_path``).
    # Before this, only ``slack:`` was accepted, so learn_add failed with
    # HTTP 400 "unknown session" from every OTHER channel (Telegram /
    # Discord / Webex / WeCom) even though the session is fully identified
    # (#1268). The bare Slack thread_ts shim stays for legacy native-Slack
    # keys. Incognito/temporary sessions are still blocked by each route's
    # live-slot policy check (Slack is the only channel with that concept),
    # so widening the namespace does not widen memory writes to ephemeral
    # sessions.
    is_channel_ns = is_channel_session_key(sk) or bool(SLACK_THREAD_TS_RE.match(sk))
    # Only consult the on-disk JSONL when the cheaper in-memory checks all
    # fail. ``_probe_persisted_session()`` performs synchronous filesystem
    # I/O (path resolution plus a bounded metadata head read), so it runs
    # via ``asyncio.to_thread`` — never on the event loop (AUTOSDE
    # ``no-blocking-call-on-event-loop``) — and only on this rare recovery
    # path, leaving the common live-slot path free of both I/O and a thread
    # hop. One composed call answers BOTH questions (does the session
    # exist, and may it touch memory) from a single path resolution, so the
    # two decisions can never be made about different files.
    if not (in_slots or in_restricted or is_channel_ns):
        exists, persisted_mode = await asyncio.to_thread(_probe_persisted_session, slot_name)
        if not exists:
            # Slot may have been evicted from memory (idle sweep,
            # gateway restart) while the MCP subprocess keeps its
            # original KIROCREW_SESSION_KEY. No session JSONL means
            # the key genuinely does not belong to any established
            # session. (Presence does NOT imply the session is
            # non-ephemeral — every memory_mode writes a transcript —
            # which is what ``persisted_mode`` below settles.)
            _sel().log_api_access(
                caller=sk,
                operation=operation,
                outcome="denied",
                source="dashboard",
                resources="unknown_session",
            )
            return web.json_response(
                {"error": "unknown session", "code": "unknown_session"},
                status=400,
            )
        if persisted_mode is None or blocks_persisted_mode(persisted_mode):
            # Archiving a tab drops the slot AND discards its
            # ``_restricted_keys`` entry while leaving the transcript —
            # and its ``memory_mode`` marker — on disk, so the two
            # in-memory checks above cannot see that this session is
            # ephemeral. The persisted mode is the only remaining
            # evidence. ``None`` means the header was unreadable, which
            # is NOT evidence that the call is allowed: append() writes
            # the metadata line at file creation, so a normal session
            # always has one. Fail closed.
            _sel().log_api_access(
                caller=sk,
                operation=operation,
                outcome="denied",
                source="dashboard",
                resources="restricted_session_block",
            )
            return web.json_response(
                {
                    "error": "Memory writes are not allowed in this session mode.",
                    # Machine-readable per the error-code contract; matches
                    # the code already used for this condition at
                    # handlers/memory.py's restricted-session refusal.
                    "code": "restricted_session",
                },
                status=403,
            )
        # JSONL-fallback is the sole reason the call is permitted.
        # Audit it as an allow decision so session-recovery
        # authorization is traceable alongside the deny path above.
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="jsonl_fallback_recovery",
        )
    elif in_slots:
        # Live in-memory slot — the common happy path. Audit so that
        # every permission decision on this branch is traceable.
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="live_slot",
        )
    elif in_restricted:
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="restricted_key",
        )
    else:  # is_channel_ns
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="channel_namespace",
        )
    return None


async def api_lessons_create(request: web.Request) -> web.Response:
    """POST /api/lessons — add a lesson (vector store or JSONL fallback)."""
    from kiro_crew.learn import Lesson  # noqa: F811

    state: DashboardState = request.app["state"]
    # Default cap: lesson fields are short strings (MAX_SHORT_STRING-bounded).
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Block lesson writes from restricted (incognito/temporary/guest) sessions.
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state,
        sk,
        "learn_add",
        blocks_persisted_mode=is_incognito_transcript,
    )
    if refusal is not None:
        return refusal
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
    # Restricts the lesson to one repository; absent means it applies everywhere.
    # Both write paths carry it, so the JSONL fallback store gates identically to
    # the vector store rather than injecting a scoped lesson the other withholds.
    repo_scope = cleaned.get("repo_scope") or None
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
        result = await asyncio.to_thread(
            vs.write_lesson,
            rule,
            category,
            negative,
            "user_explicit",
            rule_emb,
            rule_emb_generation,
            repo_scope,
        )
        # Sweep ONLY when the lesson actually landed. The write declines for a value
        # its preflight refuses (reachable now that ``negative`` is forwarded here at
        # all -- this call site passed a literal None before) and for a dedup refusal.
        # The result used to be discarded, so a refused write still ran the sweep
        # below, and _resolve_and_supersede would delete_semantic an older
        # contradicted lesson whose "replacement" was never stored -- destroying a
        # lesson on a request that persisted nothing, under HTTP 200. Superseding on
        # the authority of a write that did not happen is wrong for every declining
        # outcome, so gate on ``wrote`` rather than on the cause.
        outcome = result.outcome.value
        reason = result.reason
        stored = result.stored
        if result.wrote:
            candidates = await asyncio.to_thread(
                vs.find_contradiction_candidates, rule, 0.4, 0.85, rule_emb, repo_scope
            )
            if candidates:
                # Fire-and-forget via this module's _background_tasks
                # pattern. The sweep only supersedes OTHER (older) lessons, never
                # the one just written (self-match scores ~1.0, above the 0.85
                # candidate ceiling), so deferring it is safe. No retry/queue: a
                # missed sweep self-heals on the next learn_add touching the topic.
                task = asyncio.create_task(_resolve_and_supersede(state, sk, rule, candidates, vs))
                state._background_tasks.add(task)
                task.add_done_callback(state._background_tasks.discard)
    else:
        lesson = Lesson(
            rule=rule,
            category=category,
            negative=negative,
            repo_scope=repo_scope,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        store = (
            _get_lessons(state, cleaned.get("workspace"))
            if scope == "workspace"
            else (state.lessons)
        )
        # save_or_enrich, not save: a re-submit of a stored rule carrying a new
        # NOT-clause has to attach it rather than be skipped as a duplicate.
        # Off the loop because it reads the file and rewrites it whole -- the
        # same reason dashboard/ws.py offloads load_all.
        #
        # This store answers with the same three words the vector store's outcome uses
        # (inserted / enriched / unchanged) and validates no content, so it has no
        # refusing outcome to report. Its value is echoed as-is: ``state.lessons`` is a
        # real ``LessonStore`` at every construction site, and its ``save_or_enrich``
        # is annotated ``-> str`` with three string-literal returns, so there is
        # nothing here for a filter to catch. ``test_lesson_write_outcome`` pins
        # LessonWriteOutcome's wire values against those three words, so the two
        # stores cannot drift apart in silence.
        outcome = await asyncio.to_thread(store.save_or_enrich, lesson)
        reason = None
        stored = True
    # Refreshed unconditionally, and deliberately so. An earlier revision of this
    # change gated the push on the write having landed, which is wrong: a DECLINING
    # outcome can still have mutated the store. ``write_lesson``'s second pass
    # DELETES a row it supersedes and keeps scanning, so with a containment chain
    # (A inside R inside B) whose rows are visited A-first -- and the scan order is
    # effectively random, since get_lessons orders by md5 key -- A is removed and the
    # call then returns ``deduped`` for B. The store changed while ``wrote`` is False,
    # so gating on it left connected dashboards showing a lesson that is gone.
    # Reporting mutation separately would buy nothing over refreshing always: an extra
    # refresh on a no-op re-submit costs a redundant list fetch, a missed one shows
    # deleted data.
    state.push_refresh("lessons")
    # ``ok`` answers the question the caller actually asked -- is the lesson I
    # submitted in the store -- so it stays true for a no-op re-submit (it is stored,
    # there was simply nothing to write) and turns false when a dedup rule or
    # validation kept it out. It used to be an unconditional true, which told the
    # caller its lesson was saved even when the store had refused the value; the
    # ``learn_add`` tool and the CLI both reported "Saved" on that response.
    # ``outcome`` and ``reason`` are additive, so a client that only reads ``ok``
    # keeps working.
    return web.json_response({"ok": stored, "outcome": outcome, "reason": reason})


async def api_lessons_delete(request: web.Request) -> web.Response:
    """DELETE /api/lessons — remove lessons by substring."""
    state: DashboardState = request.app["state"]
    # Require the SAME session recognition as ``api_lessons_create``, via the
    # shared ``_recognize_session`` gate. Before this gate, deleting a lesson
    # was LESS protected than adding one: a key that create rejects with HTTP
    # 400 "unknown session" (forged, or a fresh background session whose
    # transcript hasn't flushed yet) could still substring-delete any durable
    # lesson. That asymmetry also breaks the remove-then-re-add consolidation
    # pattern non-atomically — the destructive remove succeeds, then the
    # re-add is refused, and the lesson is lost. Gating delete the same way
    # makes the pattern fail closed at step one.
    #
    # Policy differences from create are carried by the gate's parameters:
    # incognito sessions MAY delete (an active user action), only temporary
    # sessions are blocked — both for live slots (``_blocks_reads_session``
    # below) and, on the archived-session recovery path, via the persisted
    # memory-mode probe.
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state,
        sk,
        "lessons.delete",
        blocks_persisted_mode=_is_temporary_transcript,
    )
    if refusal is not None:
        return refusal
    # Block lesson deletes from live temporary sessions only.
    # Incognito allows learn_remove (active user action).
    if _blocks_reads_session(state, request):
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
    # Default cap: the body is a rule substring plus scope/workspace selectors.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    rule_sub = body.get("rule", "").strip()
    if not rule_sub:
        return web.json_response({"error": "rule substring required"}, status=400)
    scope = body.get("scope", "global")
    # Delete from vector store if active, else JSONL
    vs = _get_memory(state).vector_store
    vs_lessons = await asyncio.to_thread(vs.get_lessons) if vs else None
    if vs_lessons:
        ok = await asyncio.to_thread(vs.delete_lesson, rule_sub)
    else:
        store = (
            _get_lessons(state, body.get("workspace")) if scope == "workspace" else (state.lessons)
        )
        # Off the loop. remove() now takes the store's shared lock, which a worker
        # thread can be holding across file I/O for a concurrent save_or_enrich --
        # so calling it inline would let one lessons write stall every task on the
        # event loop. Same reason api_lessons_create offloads its write.
        ok = await asyncio.to_thread(store.remove, rule_sub)
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
    # Secret-grant metadata is owner-view only (see the field comment below).
    # This is a read-path CLASSIFICATION on the dashboard's polling endpoint,
    # deliberately NOT SEL-audited per decision: the panel refreshes every few
    # seconds, so a per-poll event would flood the log without adding signal,
    # and no secret VALUE is ever serialized here (names only). Every
    # privileged grant MUTATION (request, approve, deny, revoke, and both
    # denial branches) writes its own SEL event.
    _owner_view = is_owner_dashboard_request(request)
    data = [
        {
            "id": j.id,
            "name": redact_credentials(redact_exfiltration_urls(j.name)[0])[0],
            "message": redact_credentials(redact_exfiltration_urls(j.message)[0])[0],
            "enabled": j.enabled,
            "schedule": redact_credentials(
                redact_exfiltration_urls(
                    format_schedule(j.schedule, tz_name=j.timezone or tz_name)
                )[0]
            )[0],
            "cron_expr": j.schedule.cron_expr if j.schedule.kind == "cron" else None,
            "every_secs": j.schedule.every_secs if j.schedule.kind == "every" else None,
            "created_ts": j.created_ts or None,
            "last_status": j.last_status,
            "agent": redact_credentials(redact_exfiltration_urls(j.agent_id or "")[0])[0] or None,
            # The crews a sequence job actually wakes. Serialized because
            # `agent_sequence` takes PRECEDENCE over `agent_id` at run time, so a
            # consumer reading only `agent` would attribute such a job to the
            # wrong crew (an empty `agent_id` reads as "the default crew").
            "agent_sequence": [
                redact_credentials(redact_exfiltration_urls(a or "")[0])[0]
                for a in (j.agent_sequence or [])
            ],
            "model": redact_credentials(redact_exfiltration_urls(j.model or "")[0])[0] or None,
            "channel": redact_credentials(redact_exfiltration_urls(j.channel or "")[0])[0] or None,
            "approval_mode": redact_credentials(redact_exfiltration_urls(j.approval_mode or "")[0])[
                0
            ]
            or None,
            # The chat session that owns this job. Ownership decides chat-side
            # reachability: cron_list only shows a session its own jobs, so a job
            # whose key is empty (None here) is invisible to every chat session
            # and manageable only from this page or the CLI. Raw value on
            # purpose — the frontend decides presentation, and a derived
            # "reachable" boolean would be a second encoding of the same fact.
            "session_key": redact_credentials(redact_exfiltration_urls(j.session_key or "")[0])[0]
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
            # Grant metadata only — env-var names and vault secret NAMES;
            # plaintext values never leave the vault. Owner-only even so: a
            # non-owner dashboard token (an allowed Slack user's !dashboard
            # session) must not learn which vault entries exist or approve
            # targets — the same boundary the grant endpoint enforces. Keys
            # AND values are scanned like every sibling field: the store is
            # agent-writable and the read path loads these dicts verbatim,
            # so a mapping planted directly in the store must not carry
            # credential- or exfil-URL-shaped content to the dashboard.
            "secret_env": (_redacted_grant_map(j.secret_env) if _owner_view else None),
            "secret_env_pending": (
                _redacted_grant_map(j.secret_env_pending) if _owner_view else None
            ),
            "secret_env_pending_ts": (j.secret_env_pending_ts or None) if _owner_view else None,
            "last_result": redact_credentials(redact_exfiltration_urls(j.last_result or "")[0])[0]
            or None,
            "last_error": redact_credentials(redact_exfiltration_urls(j.last_error or "")[0])[0]
            or None,
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
# binds a Lock to the loop it first waits on) — loop-bound via the shared
# LoopBoundLock (#4800).
_cron_folders_lock = LoopBoundLock()


def _get_cron_folders_lock() -> LoopBoundLock:
    """Return the cron-folders lock (loop-bound; rebinds per running loop)."""
    return _cron_folders_lock


async def api_cron_folders(request: web.Request) -> web.Response:
    """GET /api/cron-folders — list all cron folders."""
    state: DashboardState = request.app["state"]
    # Serialize a shallow snapshot, not the live list: rename_cron_folder
    # mutates a folder dict's "name" in place, so encoding state._cron_folders
    # by reference could interleave with a concurrent rename and surface a torn
    # or stale name. Copying each dict gives the encoder a stable read. This
    # mirrors the chat-folders GET (api_chat_folders), which likewise does not
    # return the live list — it builds a fresh list off-thread.
    return web.json_response([dict(f) for f in state._cron_folders])


async def api_cron_folders_create(request: web.Request) -> web.Response:
    """POST /api/cron-folders — create a new cron folder."""
    state: DashboardState = request.app["state"]
    # Default cap: the body is a single short folder name.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if not isinstance(body.get("name"), str):
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
    if (_e := _invalid_path_id_response(folder_id, "folder_id")) is not None:
        return _e
    # Default cap: the body is a single short folder name.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if not isinstance(body.get("name"), str):
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
    if (_e := _invalid_path_id_response(folder_id, "folder_id")) is not None:
        return _e
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
            caller=sk,
            operation="lessons.list",
            outcome="denied",
            source="dashboard",
            resources=sk,
        )
        return web.json_response({"lessons": []})
    workspace = request.query.get("workspace")

    def _safe_lesson(rule: object, category: object, ts: object) -> dict:
        """One sanitization chokepoint for every branch of this endpoint.

        Lesson rows can carry consolidation (LLM) or import output: normalize
        the category through the shared helper (display policy, strict=False)
        so this surface cannot drift from the write-path rules, and redact
        BOTH prose fields via the shared chain like every other agent-derived
        string this handler returns -- an imported row can carry a credential
        in either field. The JSONL store loads ``rule`` without type
        validation, so a malformed row can carry a non-string here; stringify
        before the redaction rather than crashing the endpoint.
        """
        if not isinstance(rule, str):
            rule = str(rule)
        safe_rule = _redact_memory_field(rule)
        safe_category = _redact_memory_field(normalize_lesson_category(category, strict=False))
        return {"rule": safe_rule, "category": safe_category, "ts": ts}

    # Read from vector store if it has lessons, else JSONL
    vs = _get_memory(state).vector_store
    vs_lessons = await asyncio.to_thread(vs.get_lessons) if vs else None
    if vs_lessons:
        # Deferred import: ``vector_memory`` pulls snowballstemmer plus the
        # optional numpy/faiss imports, and this helper is the handler's only
        # use of it, on one dashboard read path.
        from kiro_crew.vector_memory import _lesson_display_text

        data = []
        for e in vs_lessons[-50:]:
            try:
                decoded = json.loads(e["value_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            # Rendered text for either storage shape: mapping-shaped rows
            # (write_lesson's format and the onboarding import's) would otherwise
            # ship a nested object where the dashboard expects a string. A row with
            # no lesson shape falls back to str() rather than being dropped, so it
            # stays listed and therefore deletable -- delete_lesson needs a
            # substring, and this list is the only surface that can show it. The
            # memory graph applies the same policy for the same reason.
            rule = _lesson_display_text(decoded) or str(decoded)
            raw_category = decoded.get("category") if isinstance(decoded, dict) else None
            data.append(_safe_lesson(rule, raw_category, e.get("updated_at", "")))
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
        data = [_safe_lesson(le.rule, le.category, le.ts) for le in global_lessons[-50:]]
    return web.json_response({"lessons": data})

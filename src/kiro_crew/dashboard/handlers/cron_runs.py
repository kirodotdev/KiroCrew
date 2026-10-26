"""Running a cron job and reading what its runs did.

Run now, cancel, acknowledge a notification and open the last result in a chat,
and the run history of one job or of every job.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import sys

from aiohttp import web

from kiro_crew.cron import CronPauseNotSaved, CronStoreBusy, CronStoreUnreadable
from kiro_crew.dashboard.chat_persistence import _restore_dismissed_source_links
from kiro_crew.dashboard.cron_inject import (
    apply_cron_turn_timeout,
    hydrate_slot_from_history,
    inject_cron_result_to_dashboard,
    prefetch_cron_dismissed,
    restore_cron_turn_timeout,
)
from kiro_crew.dashboard.handlers._shared import (
    _owner_denial_response,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.cron_shared import (
    _CRON_BUSY_BODY,
    _CRON_BUSY_STATUS,
    _app_caller,
    _audit_app_cron,
    _cron_unreadable_response,
    _invalid_path_id_response,
    _refuse_foreign_app_job,
)
from kiro_crew.dashboard.slot_ownership import app_holds_gateway_key
from kiro_crew.dashboard.state import DashboardState, SlotOrigin, note_crew_log_class
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from ._shared import guard_owner_surface_routes, read_bounded_json


async def api_cron_run(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/run — trigger immediate execution."""
    if request.get("app") == "":
        owner_denied = await require_owner_dashboard_request(request, "crons.run")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.run")
    if app_denied is not None:
        return app_denied
    # Freshness-guaranteed lookup: this endpoint is handed a job id minted by
    # ANOTHER process (`kirocrew cron add`, the MCP cron_add tool), which writes
    # crons.json directly. The cache-only `list_jobs()` would not see that job
    # until the timer tick refreshes the in-memory snapshot (≤_TIMER_POLL_SECS),
    # so triggering a just-created job 404'd for up to that long. Same rationale
    # as the list GET (api_crons); the read runs in a worker thread, so the loop is
    # not blocked.
    job = await state.crons.get_job_async(job_id)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    # Reject if a run is already in flight: a second overlapping run would
    # orphan the prior task's handle (nothing could track, cancel
    # or join it). The check-and-claim below is atomic: there is no await between
    # the guard, run_job's claim and attach_run_task, so the single-threaded
    # event loop cannot interleave a second request into this critical section.
    # (The lookup above awaits, so two concurrent requests can both reach the
    # guard — but only one can pass it, because the guard and the claim are not
    # separated by an await.)
    #
    # A tracked task that has already finished is NOT a run in flight, whatever
    # the claim says: a run whose task ends without reaching
    # _run_job_isolated's finally leaves its claim stored with nothing on that
    # path to release it, and this guard alone would then refuse every manual
    # run of the job until the reaper sweep meets the finished task (it does
    # the same release, once a sweep). Drop such leftovers first; the call is
    # synchronous, so the check-and-claim stays await-free, and a task still
    # running keeps the 409 below.
    state.crons.discard_finished_run(job_id)
    if state.crons.is_running(job_id):
        return web.json_response({"error": "job is already running"}, status=409)
    # run_job claims the job synchronously while the call is evaluated; the
    # wrapper task is handed to the claim on the same line so cancel() can
    # reach a run still parked in its store refresh.
    task = asyncio.create_task(state.crons.run_job(job_id))
    state.crons.attach_run_task(job_id, task)
    state.push_refresh("crons")
    safe_name = redact_credentials(redact_exfiltration_urls(job.name)[0])[0]
    return web.json_response({"ok": True, "name": safe_name})


async def api_cron_cancel(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/cancel — cancel a running execution."""
    if request.get("app") == "":
        owner_denied = await require_owner_dashboard_request(request, "crons.cancel")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.cancel")
    if app_denied is not None:
        return app_denied
    jobs = state.crons.list_jobs(include_disabled=True)
    job = next((j for j in jobs if j.id == job_id), None)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    try:
        cancelled = await state.crons.cancel(job_id)
    except CronPauseNotSaved as exc:
        # The run is cancelled; only the one-shot's pause did not reach the store.
        state.push_refresh("crons")
        return web.json_response(
            {"error": str(exc), "code": "cron_pause_not_saved", "retryable": False},
            status=409,
        )
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
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.to_chat")
    if app_denied is not None:
        return app_denied
    slot_name = f"cron-{job_id}"
    # The job's tab key is held by an app's slot: it is not adopted (see
    # slot_ownership.app_holds_gateway_key), so there is no tab to open.
    if app_holds_gateway_key(
        state, slot_name, "cron.to_chat", actor=str(request.get("app") or "dashboard")
    ):
        return web.json_response(
            {"error": "this job's chat tab is unavailable", "code": "cron_slot_unavailable"},
            status=409,
        )
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
            state,
            job,
            job.last_result or "",
            history=history,
            dismissed=await prefetch_cron_dismissed(state, job.id),
            include_prompt=False,
        )
        await restore_cron_turn_timeout(state, job.id)
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
                # A cron link is exempt from the channel class, so this records nothing
                # in practice -- it is here so that EVERY assignment site reaches the
                # recorder and the derived pin needs no exception for this one.
                note_crew_log_class(state, slot)
                hydrate_slot_from_history(slot, history)
                # Mark dismissed-UNHYDRATED before the off-loop read: the slot is
                # now bound + dirty with an empty in-memory set, and a periodic
                # flush during the await would otherwise serialize [] over the
                # transcript's real dismissals. With the flag False any such flush
                # carries the on-disk line forward instead. A readable restore
                # below then hydrates it; an unreadable read leaves it deferred.
                #
                # CLEAR the in-memory set here rather than trusting
                # ``get_or_create_slot`` to have done it: this slot object may be
                # a REUSED ``cron-{id}`` slot that still carries a PRIOR binding's
                # dismissal. If that stale key survived and the metadata read came
                # back UNREADABLE (so the authoritative restore below is skipped),
                # the deferred union-carry flush would fold the foreign key into
                # this ``session_key`` transcript and hide its matching chip. An
                # empty set means the union carries only the transcript's own
                # on-disk dismissals; the readable restore replaces it wholesale.
                slot._dismissed_source_links = set()
                slot.invalidate_source_links()
                slot._dismissed_hydrated = False
                # The tab's own turn time limit is unread for the same reason:
                # its saves carry the on-disk value until the read below lands.
                slot.turn_timeout_secs = None
                slot._turn_timeout_hydrated = False
                if state.conversation_log is not None:
                    try:
                        _meta, _readable = await asyncio.to_thread(
                            state.conversation_log.get_metadata_status, session_key
                        )
                    except Exception:
                        _meta, _readable = {}, False
                    if _readable:
                        _restore_dismissed_source_links(slot, _meta.get("dismissed_source_links"))
                        # The read is a rebind window: a limit the user set while
                        # it ran wins over the older line, and a tab now bound to
                        # another transcript takes nothing from this one.
                        if slot.linked_session_key == session_key:
                            apply_cron_turn_timeout(slot, _meta)
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


async def api_cron_ack(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/ack — acknowledge a cron notification."""
    if request.get("app") == "":
        owner_denied = await require_owner_dashboard_request(request, "crons.ack")
        if owner_denied is not None:
            return owner_denied
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.ack")
    if app_denied is not None:
        return app_denied
    # Default cap: the body is a short summary + notification ts. allow_absent
    # keeps the missing-body-means-defaults contract; see api_cron_enable.
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    summary = body.get("summary", "acknowledged")
    notification_ts = body.get("ts", "")
    # An app may mark read only a notification of the job it just passed:
    # the same ts / kind / job_id match api_notification_unack makes.
    app = _app_caller(request)
    if notification_ts and app:
        if not any(
            n.get("ts") == notification_ts and n.get("kind") == "cron" and n.get("job_id") == job_id
            for n in state._notification_log
        ):
            _audit_app_cron(app, "crons.ack", "denied", job_id)
            return _owner_denial_response(request)
    try:
        ok = await state.crons.ack_job_async(job_id, summary)
    except CronStoreBusy:
        return web.json_response(_CRON_BUSY_BODY, status=_CRON_BUSY_STATUS)
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    if notification_ts:
        await state.ack_notification(notification_ts)
    return web.json_response({"ok": ok})


# The most runs one history page returns. The in-tree callers ask for 10, 21 and
# 40 (JobLogsView, ExecutionsView, mcp_cron_list_render).
_HISTORY_PAGE_MAX = 200


def _history_page(request: web.Request) -> tuple[int, int]:
    """``(limit, offset)`` for a history read, folded into a real page.

    Both reads end in a Python list slice, where a negative bound counts from
    the END: ``offset=-3`` would return the three oldest runs and ``limit=-1``
    every run but one. So ``offset`` folds to ``>= 0`` and ``limit`` to
    ``[1, _HISTORY_PAGE_MAX]``. A non-integer keeps the default, as before.
    """
    try:
        limit = int(request.query.get("limit", "20"))
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        offset = 0
    return max(1, min(limit, _HISTORY_PAGE_MAX)), max(0, offset)


async def api_cron_history(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/history — paginated execution history (no trace)."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.history")
    if app_denied is not None:
        return app_denied
    limit, offset = _history_page(request)
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
    app_denied = await _refuse_foreign_app_job(request, state, [job_id], "crons.history_detail")
    if app_denied is not None:
        return app_denied
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
    limit, offset = _history_page(request)
    app = _app_caller(request)
    if app:
        # An app sees only the runs of jobs it owns, judged by the same
        # ``created_by`` stamp ``_refuse_foreign_app_job`` reads. The filter
        # runs before pagination so ``total`` counts the app's own runs; the
        # index is capped by ``cron_max_index_records``, so the full read is
        # bounded. Function-local import for the reason ``api_crons`` gives.
        from kiro_crew.apps.cron_sdk import app_owner_name

        owned = {
            j.id
            for j in state.crons.list_jobs(include_disabled=True)
            if app_owner_name(j.created_by) == app
        }
        rows, _ = await state.crons.get_history().get_all_history(
            job_id=job_id, limit=sys.maxsize, offset=0
        )
        rows = [row for row in rows if row.get("job_id") in owned]
        total = len(rows)
        runs = rows[offset : offset + limit]
        # Record the ownership scoping decision: this branch applies an
        # access-control filter to an app token's read, and token-auth events
        # log only authentication while dashboard GET auditing is excluded, so
        # without this SEL row no event records that the app was confined to its
        # own runs. One row per request on this on-demand (not polled) route.
        _audit_app_cron(app, "crons.history_all", "allowed", f"rows={total}")
    else:
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


# Every api_* handler in this module is an owner surface, so a private member's
# internal call is refused before it runs.
guard_owner_surface_routes(globals(), member_scoped=frozenset())

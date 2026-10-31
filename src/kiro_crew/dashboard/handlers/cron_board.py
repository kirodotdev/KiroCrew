"""The cron page's job list and the folders it groups jobs into.

``GET /api/crons`` and the cron-folder routes, which serialize their mutations
under one loop-bound lock.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.cron_shared import (
    _app_caller,
    _audit_app_cron,
    _invalid_path_id_response,
    _redacted_grant_map,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import MAX_SHORT_STRING

from ._shared import guard_owner_surface_routes, read_bounded_json

logger = logging.getLogger("kiro_crew.dashboard.handlers.cron")


def _job_snapshot(job: Any) -> Any:
    """A copy of ``job`` the list build can read off the loop.

    Shallow, plus fresh containers for the fields that are lists or dicts and
    the schedule, which is the only nested object the build reads. Strings and
    numbers are immutable, so sharing them is safe.
    """
    snap = copy.copy(job)
    for name in ("skip_dates", "agent_sequence"):
        value = getattr(snap, name, None)
        if isinstance(value, list):
            setattr(snap, name, list(value))
    for name in ("secret_env", "secret_env_pending"):
        value = getattr(snap, name, None)
        if isinstance(value, dict):
            setattr(snap, name, dict(value))
    snap.schedule = copy.copy(job.schedule)
    return snap


async def api_crons(request: web.Request) -> web.Response:
    # Function-local for the reason the helpers in cron_shared are: importing
    # `kiro_crew.apps.cron_sdk` executes `kiro_crew.apps.__init__`, which pulls
    # `bridges` and its documented mcp_cron cycle. Nothing on the boot path
    # needs this symbol, so paying for it per request keeps that cycle out of
    # module import order entirely.
    from kiro_crew.apps.cron_sdk import app_owner_name
    from kiro_crew.cron import (  # noqa: F811
        compute_next_run_ts,
        effective_wake_budget,
        format_schedule,
        get_local_tz,
    )
    from kiro_crew.cron_service.schedule import backoff_delaying_next_run

    def _r(text: str) -> str:
        # The two redaction passes every free-text field below goes through.
        return redact_credentials(redact_exfiltration_urls(text)[0])[0]

    state: DashboardState = request.app["state"]
    # Freshness-guaranteed list for the user-facing GET: offloads a locked
    # _sync() + snapshot to a worker thread so a cron just created by a separate
    # process (CLI / MCP) shows up immediately, without ever blocking the event
    # loop with the store read/hash. The hot per-connection status push and the
    # other mutation handlers keep using the cache-only list_jobs().
    jobs = await state.crons.list_jobs_async(include_disabled=True)
    # An app token reaches this list route, but like every per-job cron route
    # it sees only the jobs it created, judged by the same host-written
    # ``created_by`` ``app:<name>`` stamp ``_refuse_foreign_app_job`` reads.
    # Without this the dashboard's polling endpoint would hand an app every
    # job's ``message``, ``last_result``, ``last_error``, ``command`` and
    # ``script`` -- exactly the run data the per-job gates protect --
    # leaving the trust boundary open on the one route that returns it in bulk.
    # Filtered here so every field below is already scoped. The app-only branch
    # writes one SEL row recording the ownership scoping decision: token-auth
    # events log only authentication and dashboard GET auditing is excluded, so
    # this is the only record that the app was confined to its own jobs. The
    # row is written ONLY on the app path -- the owner, dashboard and
    # internal-secret callers (``app == ""``) skip the filter and the audit, so
    # the owner-view poll stays unaudited and unaffected, as before.
    app = _app_caller(request)
    if app:
        jobs = [j for j in jobs if app_owner_name(j.created_by) == app]
        _audit_app_cron(app, "crons.list", "allowed", f"jobs={len(jobs)}")
    # A crewmate's goal check-ins are controlled only by its Goals tab switches,
    # so neither the Schedule page nor a crewmate's Schedules tab lists them;
    # the store still runs them. Only those: every other managed job stays.
    from kiro_crew.dashboard.goal_reminder import is_reminder_job

    jobs = [j for j in jobs if not is_reminder_job(j)]
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
    # The run claims and the slot registry belong to the loop, so they are read
    # here; everything else is built in a worker thread below. Building every
    # row (each free-text field through both redaction passes) and encoding the
    # body took hundreds of milliseconds on the loop for a few hundred jobs with
    # long results, and the Schedule page reads this list often.
    live = {
        j.id: (
            state.has_slot(f"cron-{j.id}"),
            state.crons.is_running(j.id),
            state.crons.running_since(j.id),
        )
        for j in jobs
    }
    # Copies, taken on the loop: a run or a mutator may write a job's fields
    # (or fill one of its lists and dicts) while the worker reads them.
    jobs = [_job_snapshot(j) for j in jobs]

    def _build() -> str:
        data = [
            {
                "id": j.id,
                "name": _r(j.name),
                "message": _r(j.message),
                "enabled": j.enabled,
                "schedule": _r(format_schedule(j.schedule, tz_name=j.timezone or tz_name)),
                "cron_expr": j.schedule.cron_expr if j.schedule.kind == "cron" else None,
                "every_secs": j.schedule.every_secs if j.schedule.kind == "every" else None,
                "created_ts": j.created_ts or None,
                "last_status": j.last_status,
                # The installed app that owns this job, or None for a person-owned
                # one. Derived from `created_by` rather than serialized raw: that
                # field doubles as a human creator's Slack user ID, which this
                # endpoint has no reason to disclose, and the app reading is the
                # only one a consumer here wants. Host-written at creation, so an
                # app cannot claim another app's jobs by supplying it.
                "app": app_owner_name(j.created_by) or None,
                # Whether the USER paused this job, as opposed to execution pausing
                # it after repeated failures. Both land as `enabled=False`, so
                # `enabled` alone cannot tell them apart -- and the difference is the
                # whole signal for a consumer judging health: a job the user paused
                # on purpose is not a health signal, while one auto-paused after
                # consecutive failures is the WORST one, which `unhealthy_jobs_from_disk`
                # already treats that way by skipping only user pauses.
                "user_paused": j.user_paused,
                "agent": _r(j.agent_id or "") or None,
                "member_id": j.member_id or None,
                "memory_store": j.memory_store or None,
                # The crews a sequence job actually wakes. Serialized because
                # `agent_sequence` takes PRECEDENCE over `agent_id` at run time, so a
                # consumer reading only `agent` would attribute such a job to the
                # wrong crew (an empty `agent_id` reads as "the default crew").
                "agent_sequence": [_r(a or "") for a in (j.agent_sequence or [])],
                "model": _r(j.model or "") or None,
                "channel": _r(j.channel or "") or None,
                "approval_mode": _r(j.approval_mode or "") or None,
                # The chat session that owns this job. Ownership decides chat-side
                # reachability: cron_list only shows a session its own jobs, so a job
                # whose key is empty (None here) is invisible to every chat session
                # and manageable only from this page or the CLI. Raw value on
                # purpose — the frontend decides presentation, and a derived
                # "reachable" boolean would be a second encoding of the same fact.
                "session_key": _r(j.session_key or "") or None,
                "silent": j.silent,
                "strict_schedule": j.strict_schedule,
                "hide_in_chat": j.hide_in_chat,
                # Returned so the edit form can show the job's real setting instead
                # of defaulting the control to off and silently clearing the flag on
                # the next save.
                "minimal_context": j.minimal_context,
                # What the service enforces, not the raw field: a stored value
                # outside its range runs under the default instead.
                "timeout_secs": effective_wake_budget(j),
                # Same reason: without it a form control for the flag would default
                # to the store's True and a save would silently re-enable the
                # persistent session on a job the user set to ephemeral.
                "persistent_session": j.persistent_session,
                # The edit form's auto-pause field reads it so a save shows the
                # job's real limit; 0 means the job never auto-pauses.
                "auto_pause_after_failures": j.auto_pause_after_failures,
                "alert_after_failures": j.alert_after_failures,
                "folder_id": j.folder_id,
                "chat_folder_id": j.chat_folder_id,
                # The Schedule-page template this job was seeded from, or None. A
                # stable catalog id (e.g. "error-digest"), not user free-text, so
                # it is returned as-is; the frontend matches it against the live
                # SCHEDULE_PRESETS to decide whether the source template moved.
                "source_preset": _r(j.source_preset or "") or None,
                # The template's prompt AS IT WAS at save time. The frontend
                # compares THIS against the live preset prompt (template moved?),
                # not the job's current message (which the user may have edited),
                # so the hint attributes the change to the template. Redacted with
                # the same pipeline as every other free-text field on this dict:
                # it is a client-settable POST field, so it cannot bypass the
                # dashboard's credential/exfiltration redaction. None when the job
                # carries no template lineage.
                "source_template_prompt": _r(j.source_template_prompt or "") or None,
                "last_run_ts": j.last_run_ts,
                "last_retry_count": j.last_retry_count,
                "last_retry_run_ts": j.last_retry_run_ts,
                "has_result": bool(j.last_result),
                "has_slot": live[j.id][0],
                "next_run_ts": compute_next_run_ts(j, now=now),
                # Why `next_run_ts` may sit later than the schedule says: a job
                # that keeps failing with auto-pause off (or a raised limit) backs
                # off, and compute_next_run_ts already returns the shifted time.
                # The raw epoch, not a "backing off" boolean, so the page compares
                # it against its own clock and the note clears once it passes.
                "consecutive_failures": j.consecutive_failures,
                # Only when the backoff is what moved the next run: null for a
                # disabled job and for one whose next slot is later anyway.
                "failure_backoff_until": backoff_delaying_next_run(j, now=now),
                "timezone": _r(j.timezone or "") or None,
                "skip_dates": ([_r(d) for d in j.skip_dates] if j.skip_dates else None),
                "script": _r(j.script or "") or None,
                "command": _r(j.command or "") or None,
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
                "last_result": _r(j.last_result or "") or None,
                "last_error": _r(j.last_error or "") or None,
                "is_running": live[j.id][1],
                "running_since": live[j.id][2],
            }
            for j in jobs
        ]
        return json.dumps(
            {
                "jobs": data,
                "server_tz": _r(tz_name or "") or None,
            }
        )

    # The worker inherits this request's context, so a redaction scope the
    # caller is in applies there too.
    body = await asyncio.to_thread(_build)
    return web.Response(text=body, content_type="application/json")


# ── Cron Folders ──

# Serializes all cron-folder mutations (create/rename/delete) so concurrent
# requests cannot race on the in-memory list + disk persist cycle. The lock is
# created lazily and re-created if the running event loop changes (Python 3.10
# binds a Lock to the loop it first waits on) — loop-bound via the shared
# LoopBoundLock.
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


# Folder ids are minted server-side as 8 hex characters. A missing reference of
# that shape is a stale id, never a name to create (the MCP tool's rule too).
_FOLDER_ID_SHAPE = re.compile(r"[0-9a-f]{8}")


async def create_or_adopt_cron_folder(state: DashboardState, name: str) -> tuple[str, str | None]:
    """Return the id of the folder called ``name``, creating it when there is none.

    The app cron bridge's create path. It runs under the same lock and writes the
    same in-memory list as ``POST /api/cron-folders``, so the dashboard's next
    wholesale save keeps the folder. The name is matched again under the lock,
    case-insensitively, so two registrations racing on one missing name make one
    folder. A name several folders share is refused, as the lookup refuses it.
    Returns ``(folder_id, error)``; on an error the id is ``""``.
    """
    name = str(name or "").strip()
    if not name or len(name) > MAX_SHORT_STRING:
        return "", "folder name is empty or too long"
    if _FOLDER_ID_SHAPE.fullmatch(name):
        return "", f"cron folder not found: {name!r} looks like a folder id"
    async with _get_cron_folders_lock():
        matches = [f for f in state._cron_folders if f["name"].strip().lower() == name.lower()]
        if len(matches) > 1:
            return "", f"{len(matches)} cron folders are named {name!r}"
        if matches:
            return str(matches[0]["id"]), None
        try:
            folder = await asyncio.to_thread(state.create_cron_folder, name, uuid.uuid4().hex[:8])
        except Exception:
            logger.warning("Failed to persist cron folder create", exc_info=True)
            return "", "failed to save folder"
    state.push_refresh("crons")
    return str(folder["id"]), None


def wire_app_cron_folder_creator(state: DashboardState) -> None:
    """Let the app cron bridge create missing folders through this dashboard.

    Call only after ``state.load_cron_folders()`` (before it the in-memory list is
    empty, and a create would save over the folders on disk), and only after the
    socket binds, so the startup registration pass adds no write to the boot path.
    """

    async def _create(name: str) -> tuple[str, str | None]:
        return await create_or_adopt_cron_folder(state, name)

    state.crons.folder_creator = _create


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


# Every api_* handler in this module is an owner surface, so a private member's
# internal call is refused before it runs.
guard_owner_surface_routes(globals(), member_scoped=frozenset())

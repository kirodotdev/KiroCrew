"""Chat Status Tags — backend routes.

Builtin-app contract: ``register_routes(app: web.Application) -> None`` registering
FULL paths directly on the gateway router (see the call site in
``dashboard/server.py``: ``_mod.register_routes(app)``, single argument). This is
NOT the external-app ``AppRoute``-list contract — mixing them up produces routes
that silently never dispatch.

One resource: the reconcile prompt. ``GET`` reports the effective prompt, whether
it is the shipped default, and the default itself (so the app page can offer a
"reset" that shows what it would reset to). ``PUT`` saves a custom prompt, or
resets to the default when the body's ``prompt`` is an empty string.

Every handler is wrapped in ``_require_enabled``: builtin routes exist from gateway
startup even while the app is disabled (this one ships ``defaultEnabled`` and is an
opt-in surface), so a disabled app must not stay callable.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.builtins.chat_status_tags import settings
from kiro_crew.apps.builtins.chat_status_tags.prompts import DEFAULT_RECONCILE_PROMPT
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.manager import is_app_enabled

logger = logging.getLogger(__name__)

APP_NAME = "chat-status-tags"
_BASE = f"/api/apps/{APP_NAME}"

#: The hourly reconciler cron this app ships. The scheduler stores app crons
#: under the app-namespaced name (``_namespace(app, resource)`` in
#: ``apps/bridges.py`` = ``"{app}/{resource}"``), so the on-disk job is named
#: with this prefix, NOT the bare manifest name.
_RECONCILE_CRON = "sdlc-tag-reconcile"
_RECONCILE_JOB_NAME = f"{APP_NAME}/{_RECONCILE_CRON}"

#: The manifest's ``cron_expr`` for the reconcile job. Used as the reported
#: schedule when the live job carries none — keeps the fallback in ONE place
#: rather than duplicating the string across the status helper.
_RECONCILE_MANIFEST_SCHEDULE = "23 * * * *"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: Same cap the store enforces. Duplicated here so an oversized body is refused
#: at the boundary with a clear 400 rather than silently truncated on write.
_MAX_PROMPT_LEN = settings.MAX_PROMPT_LEN


def _require_enabled(handler: Handler) -> Handler:
    """Deny every request while the app is disabled (deny-by-default).

    ``is_app_enabled`` is a synchronous ``installed.json`` read, so it runs off the
    event loop — same treatment as the other builtin apps' gates.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


async def _json_body(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is a 400, not a 500
        return None
    return body if isinstance(body, dict) else None


def _prompt_payload() -> dict[str, Any]:
    """The shape both GET and PUT return, read off the store."""
    return {
        "prompt": settings.get_prompt(),
        "isDefault": settings.is_default(),
        "defaultPrompt": DEFAULT_RECONCILE_PROMPT,
    }


def _cron_service(request: web.Request) -> Any | None:
    """The running CronService, or None when the scheduler is unavailable.

    Reached the same way ops-mission-control reaches it (``_handle_rotation_arm``):
    the gateway hangs its shared services off ``request.app["state"]``, and the
    scheduler is ``state.crons``. Both the missing-state case (a bare aiohttp app
    in a test) and the no-scheduler case (state present, ``crons`` unset) collapse
    to None so callers have one "unavailable" branch to handle.
    """
    state = request.app.get("state")
    return getattr(state, "crons", None)


def _find_reconcile_job(cron_service: Any) -> Any | None:
    """The app's reconcile job as the scheduler holds it, or None if absent.

    Uses :class:`CronSDK`, whose ``list_jobs`` is owner-scoped (only
    ``created_by == "app:chat-status-tags"``) and includes disabled/paused jobs —
    so a paused reconcile job is still found, and a same-named job owned by
    something else is never mistaken for ours. Matches on the namespaced name the
    scheduler actually stores (``chat-status-tags/sdlc-tag-reconcile``).
    """
    sdk = CronSDK(APP_NAME, cron_service)
    for job in sdk.list_jobs():
        if getattr(job, "name", "") == _RECONCILE_JOB_NAME:
            return job
    return None


async def sync_prompt_to_job(cron_service: Any, prompt: str) -> bool:
    """Push the effective reconcile prompt into the live cron job's ``message``.

    This is the whole point of the design: operator configuration reaches the
    reconciler as the cron's OWN instructions (a trusted channel that needs no
    tool call), not as a file the agent must fetch and then distrust. Called on
    every save/reset (``_handle_put_prompt``), on startup, and after any
    re-register/heal (repair endpoint, enable-toggle) — because
    ``register_app_crons_with_service`` rebuilds the job from the IMMUTABLE
    manifest and would otherwise clobber a custom prompt back to the default.

    Owner-scoped through :class:`CronSDK.update_job_async`, so it can only ever
    touch this app's own job. Returns True when the live job was updated, False
    when the scheduler is unavailable or the job is absent (the caller still
    persisted the prompt to disk; only the live sync was skipped). Never raises
    for the "no job to update" case — a fresh install with no scheduler yet is a
    normal state, not an error.
    """
    if cron_service is None:
        return False
    job = await asyncio.to_thread(_find_reconcile_job, cron_service)
    if job is None:
        return False
    sdk = CronSDK(APP_NAME, cron_service)
    await sdk.update_job_async(job.id, message=prompt)
    return True


def _cron_status(cron_service: Any) -> dict[str, Any]:
    """Report the reconcile job's presence, enabled state, and schedule.

    ``enabled`` reflects the scheduler's effective flag (a user-paused or
    auto-paused job reads False). ``schedule`` is the job's own ``cron_expr``,
    falling back to the manifest's ``23 * * * *`` when the live job carries none
    (e.g. an interval-only job, or a field left unset). When the scheduler is
    unavailable, presence/enabled are both False and ``schedulerUnavailable`` is
    set so the page can distinguish "no scheduler" from "job missing".
    """
    if cron_service is None:
        return {
            "present": False,
            "enabled": False,
            "schedule": _RECONCILE_MANIFEST_SCHEDULE,
            "schedulerUnavailable": True,
        }
    job = _find_reconcile_job(cron_service)
    if job is None:
        return {
            "present": False,
            "enabled": False,
            "schedule": _RECONCILE_MANIFEST_SCHEDULE,
        }
    schedule = getattr(getattr(job, "schedule", None), "cron_expr", None)
    return {
        "present": True,
        "enabled": bool(getattr(job, "enabled", False)),
        "schedule": schedule or _RECONCILE_MANIFEST_SCHEDULE,
    }


async def _handle_get_prompt(request: web.Request) -> web.StreamResponse:
    payload = await asyncio.to_thread(_prompt_payload)
    # The scheduler read is a cheap in-memory list scan, but it touches
    # CronService state, so keep it off the loop alongside the store read.
    payload["cron"] = await asyncio.to_thread(_cron_status, _cron_service(request))
    return web.json_response(payload)


async def _handle_put_prompt(request: web.Request) -> web.StreamResponse:
    """Save a custom reconcile prompt, or reset to default.

    ``prompt`` must be a string. An empty string RESETS to the default (the store
    deletes the file); this is deliberate, so a client can clear the field to
    revert. Anything other than a string is a 400 — coercing would let a JSON
    ``null`` or number become a nonsense instruction the cron then follows.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    if "prompt" not in body:
        return web.json_response(
            {"error": "prompt is required", "code": "missing_required_field"}, status=400
        )
    prompt = body["prompt"]
    if not isinstance(prompt, str):
        return web.json_response(
            {"error": "prompt must be a string", "code": "invalid_field_type"}, status=400
        )
    if len(prompt) > _MAX_PROMPT_LEN:
        return web.json_response(
            {"error": f"prompt exceeds {_MAX_PROMPT_LEN} characters", "code": "value_too_long"},
            status=400,
        )

    await asyncio.to_thread(settings.set_prompt, prompt)

    # Push the now-effective prompt into the live reconcile cron's message so it
    # reaches the agent as its own instructions (the trusted channel), not as a
    # file it must fetch and distrust. The prompt is ALREADY persisted above, so
    # a scheduler-unavailable or job-absent install still succeeds — we just
    # report that the live job was not updated (jobMessageSynced=False) rather
    # than failing the save.
    effective = await asyncio.to_thread(settings.get_prompt)
    cron_service = _cron_service(request)
    synced = await sync_prompt_to_job(cron_service, effective)

    payload = await asyncio.to_thread(_prompt_payload)
    payload["cron"] = await asyncio.to_thread(_cron_status, cron_service)
    payload["jobMessageSynced"] = synced
    return web.json_response(payload)


# ── behaviour toggles ──────────────────────────────────────────────────────

#: The two toggle keys, mapping the store's snake_case flag names to the
#: camelCase the HTTP surface uses (matching the rest of this file's JSON).
_FLAG_TO_JSON = {
    "reconciler_enabled": "reconcilerEnabled",
    "auto_resume_enabled": "autoResumeEnabled",
}
_JSON_TO_FLAG = {v: k for k, v in _FLAG_TO_JSON.items()}


def _flags_payload() -> dict[str, Any]:
    """The settings body: current toggles rendered in camelCase for the client."""
    flags = settings.get_flags()
    return {json_key: flags[flag] for flag, json_key in _FLAG_TO_JSON.items()}


async def _apply_reconciler_flag(cron_service: Any, enabled: bool) -> None:
    """Pause or resume the app's reconcile cron to match ``reconcilerEnabled``.

    Reuses the SAME plumbing as ``_handle_repair_cron``: ``_find_reconcile_job``
    to locate the owner-scoped namespaced job, ``enable_job_async`` to flip its
    enabled state, and ``register_app_crons_with_service`` to heal a missing job.

    * ``enabled=True``  → resume the job (``enable_job_async(id, enabled=True)``,
      which also clears an auto-pause), or re-register it from the manifest when
      absent (the repair path's heal).
    * ``enabled=False`` → pause the job (``enable_job_async(id, enabled=False)``,
      which sets ``user_paused``); a missing job needs no action to be "off".

    Idempotent: a job already in the target state is left untouched.
    """
    job = await asyncio.to_thread(_find_reconcile_job, cron_service)
    if enabled:
        if job is None:
            # Lazy import mirrors _handle_repair_cron: apps.bridges pulls in a
            # large chunk of the app subsystem and risks an import cycle at load.
            from kiro_crew.apps.bridges import register_app_crons_with_service

            await register_app_crons_with_service(APP_NAME, cron_service)
        elif not getattr(job, "enabled", False):
            await cron_service.enable_job_async(job.id, enabled=True)
        # Re-enabling (or re-registering from the manifest) can leave the job
        # carrying the manifest default message; re-apply the stored prompt so a
        # custom one survives being toggled off and back on.
        await sync_prompt_to_job(cron_service, await asyncio.to_thread(settings.get_prompt))
    else:
        if job is not None and getattr(job, "enabled", False):
            await cron_service.enable_job_async(job.id, enabled=False)


async def _handle_get_settings(request: web.Request) -> web.StreamResponse:
    return web.json_response(await asyncio.to_thread(_flags_payload))


async def _handle_put_settings(request: web.Request) -> web.StreamResponse:
    """Set either/both toggles from a partial object; return the full fresh state.

    Body is a JSON object of ``reconcilerEnabled`` / ``autoResumeEnabled``, each
    a bool. An unknown key or a non-bool value is a 400 — coercing would let a
    JSON ``null`` or number silently turn a paid loop on or off. An empty object
    is accepted as a no-op that returns the current state.

    ``reconcilerEnabled`` additionally pauses/resumes the manifest reconcile cron
    through the same cron-service plumbing the repair endpoint uses; a
    scheduler-unavailable install yields 503 rather than a silent half-apply
    (the flag would flip on disk while the job kept running).
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    partial: dict[str, bool] = {}
    for json_key, value in body.items():
        flag = _JSON_TO_FLAG.get(json_key)
        if flag is None:
            return web.json_response(
                {"error": f"unknown key: {json_key}", "code": "unknown_field"}, status=400
            )
        if not isinstance(value, bool):
            return web.json_response(
                {"error": f"{json_key} must be a boolean", "code": "invalid_field_type"},
                status=400,
            )
        partial[flag] = value

    # If the reconciler toggle is changing, the cron pause/resume must be
    # actionable BEFORE the flag is persisted — otherwise the on-disk state and
    # the live job diverge. Refuse with 503 when the scheduler is unavailable,
    # exactly as the repair path does, and write nothing.
    cron_service = None
    prev_reconciler: bool | None = None
    if "reconciler_enabled" in partial:
        cron_service = _cron_service(request)
        if cron_service is None:
            return web.json_response(
                {"error": "cron service unavailable", "code": "cron_service_unavailable"},
                status=503,
            )
        prev_reconciler = bool(
            (await asyncio.to_thread(settings.get_flags)).get("reconciler_enabled", True)
        )
        await _apply_reconciler_flag(cron_service, partial["reconciler_enabled"])

    try:
        await asyncio.to_thread(settings.set_flags, **partial)
    except ValueError as exc:  # defence in depth — the boundary already validated
        return web.json_response({"error": str(exc), "code": "invalid_field_type"}, status=400)
    except OSError:
        # The flag file could not be written (read-only FS, disk full). The cron
        # may already have been mutated above — roll that back so live state and
        # stored state stay consistent, then report the failure honestly instead
        # of a 200 that lies about what was applied.
        if cron_service is not None and prev_reconciler is not None:
            try:
                await _apply_reconciler_flag(cron_service, prev_reconciler)
            except Exception:  # noqa: BLE001 — rollback is best-effort
                logger.warning(
                    "chat-status-tags: cron rollback after failed flag write also failed",
                    exc_info=True,
                )
        return web.json_response(
            {"error": "could not persist settings", "code": "settings_write_failed"},
            status=500,
        )
    return web.json_response(await asyncio.to_thread(_flags_payload))


async def _handle_repair_cron(request: web.Request) -> web.StreamResponse:
    """Re-register or re-enable the hourly reconcile cron.

    The reconcile job can go absent (never registered, or removed) or be left
    paused/disabled — either way the sidebar promotions silently stop. This heals
    both, idempotently:

    * scheduler unavailable  → 503 (``cron_service_unavailable``); nothing to act on.
    * job missing            → ``register_app_crons_with_service`` rebuilds it from
                               the shipped manifest (add-if-absent, so a concurrent
                               registrar cannot duplicate it).
    * job present but paused  → ``enable_job_async`` clears the pause / auto-pause.
    * job present and enabled → no-op.

    Returns the FRESH status after the action, so a caller sees the resulting
    state without a second GET. Safe to call repeatedly: a healthy job is left
    untouched and the response is identical.
    """
    cron_service = _cron_service(request)
    if cron_service is None:
        return web.json_response(
            {"error": "cron service unavailable", "code": "cron_service_unavailable"},
            status=503,
        )

    job = await asyncio.to_thread(_find_reconcile_job, cron_service)
    if job is None:
        # Lazy import: apps.bridges pulls in a large chunk of the app subsystem,
        # and importing it at module load risks an import cycle through the
        # gateway boot path. The handler is off the hot path, so the one-time
        # import cost here is fine.
        from kiro_crew.apps.bridges import register_app_crons_with_service

        await register_app_crons_with_service(APP_NAME, cron_service)
    elif not getattr(job, "enabled", False):
        # Present but paused/disabled — re-enable it. enable_job_async also clears
        # an execution auto-pause and resets the failure counter (see CronService).
        await cron_service.enable_job_async(job.id, enabled=True)

    # Whether we just re-registered from the manifest or re-enabled, the job's
    # message may now be the manifest default (register_app_crons_with_service
    # rebuilds from the IMMUTABLE manifest). Re-apply the stored prompt so a
    # custom one survives the repair.
    await sync_prompt_to_job(cron_service, await asyncio.to_thread(settings.get_prompt))

    fresh = await asyncio.to_thread(_cron_status, cron_service)
    return web.json_response({"ok": True, "cron": fresh})


def register_routes(app: web.Application) -> None:
    """Register Chat Status Tags' routes on the gateway application."""
    add = app.router
    add.add_get(f"{_BASE}/reconcile-prompt", _require_enabled(_handle_get_prompt))
    add.add_put(f"{_BASE}/reconcile-prompt", _require_enabled(_handle_put_prompt))
    add.add_get(f"{_BASE}/settings", _require_enabled(_handle_get_settings))
    add.add_put(f"{_BASE}/settings", _require_enabled(_handle_put_settings))
    add.add_post(f"{_BASE}/reconcile-cron/repair", _require_enabled(_handle_repair_cron))
    logger.info("chat-status-tags: routes registered")

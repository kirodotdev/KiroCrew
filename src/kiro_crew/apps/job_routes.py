"""The shared ``_jobs/*`` HTTP surface for the Job SDK.

Mounted ONCE for every app rather than per app: the routes carry ``{app}`` as a
path segment and resolve that app's :class:`JobSDK` from the process registry —
the same shape the Route Registry catch-all and the app API proxy already use.
A per-app loop would need the app list at registration time, which is not known
until the enable loop runs.

Registration must happen BEFORE ``RouteRegistry.ensure_catch_all()`` installs
``/api/apps/{app_name}/{path:.*}``, because aiohttp matches in registration
order and that catch-all would otherwise swallow ``_jobs`` and answer with the
app's own dispatch table instead.

Authorization. These paths sit inside ``/api/apps/{app}``, which
``_app_owns_path`` grants to that app's own token unconditionally, and the
conventional ``/api/apps/<app>/*`` manifest grant covers them too — so a
consuming app needs no new ``permissions.api`` entry. On top of that this
surface is deliberately OWNER-GATED like the account portal: ``start`` runs real
work with real side effects, and P1 ships with no consumer that needs an
app-token caller, so the narrower gate is the one that ships. Opening it to app
tokens is a later, deliberate change with its own review, not a default.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

from aiohttp import web

from kiro_crew.apps.job_sdk import JobError, JobRun, JobSDK, UnknownJobKind, get_sdk
from kiro_crew.apps.manager import get_app_manifest, is_app_enabled
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# The dashboard owner/session guards are imported lazily, INSIDE the request
# path, on purpose. Importing them at module scope pulls in
# ``kiro_crew.dashboard.handlers``, whose package init reaches
# ``handlers.security -> apps.routes -> apps.hooks_integration`` -- and
# ``hooks_integration`` is what imports this module to mount the routes, so a
# module-scope import closes the cycle and breaks gateway boot with a partially
# initialized module. Same reason ``cron_sdk`` defers its ``mcp_cron`` vetting
# imports. Per-request cost is a dict lookup in ``sys.modules``; by the time a
# request arrives the dashboard package is long since loaded.

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: What one store read returns, so ``_read_with_snapshots`` can pair a single
#: record and a list of them with the same snapshots without losing the type.
_T = TypeVar("_T")

#: Mounted under each app's own namespace.
_PREFIX = "/api/apps/{app}/_jobs"

#: Cap on a single ``recent`` page, so a caller cannot ask the gateway to read
#: an app's entire run history into one response.
_RECENT_MAX = 100


def _audit(app: str, operation: str, resources: str, outcome: str, *, error: str = "") -> None:
    """Best-effort SEL audit; never blocks the response."""
    try:
        sel().log_api_access(
            caller="dashboard-owner",
            operation=f"jobs.{operation}",
            outcome=outcome,
            source=app or "jobs",
            resources=resources[:200],
            error=error[:200],
        )
    except Exception:  # noqa: BLE001
        logger.debug("jobs SEL audit failed", exc_info=True)


def _safe(text: str) -> str:
    """Scrub an SDK error before it becomes a response body.

    The SDK already redacts what a runner produces; this covers the message the
    SDK itself raises, which can quote a path or a kind name supplied by the
    caller.
    """
    try:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        out, _ = redact_credentials(text)
        out, _ = redact_exfiltration_urls(out)
        return out[:500]
    except Exception:  # noqa: BLE001 - redaction must not mask the error
        return text[:500]


def _public_view(run: JobRun, cancelling: frozenset[str], live: frozenset[str]) -> dict[str, Any]:
    """The record as the browser sees it.

    ``origin`` and ``pid`` are host facts with no client meaning, so they are
    withheld. Everything else the record holds is served: P1's record has no
    caller- or runner-supplied payload to decide about, which is why there is no
    per-field withholding rule here any more.

    ``cancelling`` is one of two fields NOT read off the record. It says a cancel
    has been asked for and the worker has not reached the checkpoint where it
    records the outcome, and it comes from the SDK's live table via
    ``cancelling_and_live_ids`` because ``cancel`` deliberately writes nothing -- see that
    method for why persisting it would break the single-writer rule.
    ``cancelling`` is a REQUIRED argument rather than a defaulted one so that a
    future caller cannot quietly serve ``false`` by forgetting to pass it; a
    missing argument is a type error at the call site instead of a wrong answer
    in the response.

    ``live`` is the other, from the same table via ``cancelling_and_live_ids``:
    whether THIS gateway process holds the run in its live table -- claimed at
    start, dropped after the terminal write. A durable record can carry
    ``running`` with nothing owning it -- a terminal write that failed twice,
    or a record minted by another gateway process -- and ``live`` is the only
    signal that lets a client tell that stale record from real work; ``origin``
    and ``pid`` stay withheld. It is REQUIRED for the same reason
    ``cancelling`` is.

    A terminal run is never ``cancelling`` and never ``live``. The worker
    writes the record before ``_execute`` drops the live entry, so a read
    landing between those two would otherwise report ``status: cancelled`` and
    ``cancelling: true`` together -- the cancel both finished and still
    pending -- or a finished run as still driven. Once the status carries the
    answer there is nothing left to be pending or driven.

    ``interrupted_from`` and ``interrupt_cause`` are served for the reason they
    exist. They are not host facts -- they are the two facts a client needs to
    recover from an interruption, and a client is the only party that can act on
    them: whether the run may have committed side effects, and whether retrying
    it is even possible now. Withholding them would leave the record honest and
    the API not.
    """
    return {
        "run_id": run.run_id,
        "kind": run.kind,
        "status": run.status,
        "cancellable": run.cancellable,
        "cancelling": not run.is_terminal and run.run_id in cancelling,
        "live": not run.is_terminal and run.run_id in live,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
        "error": run.error,
        "interrupted_from": run.interrupted_from,
        "interrupt_cause": run.interrupt_cause,
    }


def _read_with_snapshots(
    sdk: JobSDK, read: Callable[[], _T]
) -> tuple[_T, frozenset[str], frozenset[str]]:
    """Run one store read and take the cancelling + live snapshots, in ONE
    off-loop hop.

    Off the loop because ``cancelling_and_live_ids`` takes the SDK's lock and
    ``_persist`` holds that same lock across a disk write, so acquiring it on
    the loop could park the gateway for the length of that write. The store
    read is already off-loop work for the same reason, so pairing them adds no
    hop.

    ONE snapshot per response, not one per row: a list endpoint would otherwise
    render each row against a different instant, so two rows could disagree about
    a cancel that landed while the page was being built. The two sets come from
    ONE lock acquisition for the same reason: taken separately, a worker
    finishing between them could serve ``cancelling: true`` and ``live: false``
    together for a non-terminal record.

    The record is read BEFORE the snapshots, deliberately. A cancel arriving
    between the two is then reported (a ``running`` record against a snapshot
    that has it) rather than missed, which is the direction that serves the user
    who just pressed the button. The reverse order would answer "not cancelling"
    about a cancel that was already in. The same order serves ``live``: a
    worker finishing between the two reads ``live: false``, which is by then
    the truth.
    """
    result = read()
    cancelling, live = sdk.cancelling_and_live_ids()
    return result, cancelling, live


def _enabled_and_permitted(app_name: str) -> tuple[bool, bool]:
    """Read enablement AND the declared ``jobs`` grant. Call off the loop.

    Authorization must NOT rest on the process registry: an SDK is published at
    enable time and lives for the gateway's life, so a grant revoked in the
    manifest afterwards would otherwise keep serving through the stale entry.
    The manifest is the authority; the registry only says where the runs are.
    """
    if not is_app_enabled(app_name):
        return False, False
    manifest = get_app_manifest(app_name)
    return True, bool(manifest and manifest.permissions.jobs)


def _guarded(
    handler: Callable[[web.Request, str, JobSDK], Awaitable[web.StreamResponse]],
) -> Handler:
    """Enabled + grant + owner check, then hand the app and SDK to the handler."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        from kiro_crew.dashboard.handlers._shared import _owner_denial_response
        from kiro_crew.dashboard.handlers.source_providers import (
            is_owner_dashboard_request,
        )

        app_name = request.match_info.get("app", "")
        if not app_name:
            return web.json_response(
                {"error": "app name missing from path", "code": "app_name_required"},
                status=400,
            )
        # Reads installed.json and the manifest from disk -- off the loop.
        enabled, permitted = await asyncio.to_thread(_enabled_and_permitted, app_name)
        if not enabled:
            _audit(app_name, "access", request.path, "denied", error="app_disabled")
            return web.json_response(
                {"error": "app is disabled", "code": "app_disabled"}, status=403
            )
        if not permitted:
            _audit(app_name, "access", request.path, "denied", error="jobs_not_granted")
            return web.json_response(
                {"error": "app has no job runtime", "code": "jobs_not_enabled"},
                status=404,
            )
        if not is_owner_dashboard_request(request):
            _audit(app_name, "access", request.path, "denied", error="non-owner")
            return _owner_denial_response(
                request, "dashboard owner required", "dashboard_owner_required"
            )
        sdk = get_sdk(app_name)
        if sdk is None:
            # Granted but nothing published: the app declared the permission and
            # is enabled, yet its context was never built (a failed enable). A
            # 404 rather than a 403 -- there is nothing here to be denied.
            return web.json_response(
                {"error": "app has no job runtime", "code": "jobs_not_enabled"},
                status=404,
            )
        # An ALLOW is a permission decision too. Auditing only denials leaves an
        # incident review able to see who was refused and not who got through.
        _audit(app_name, "access", request.path, "allowed")
        return await handler(request, app_name, sdk)

    return _wrapped


def _mutating(
    operation: str,
) -> Callable[
    [Callable[[web.Request, str, JobSDK], Awaitable[web.StreamResponse]]],
    Callable[[web.Request, str, JobSDK], Awaitable[web.StreamResponse]],
]:
    """Refuse a restricted session, then audit the outcome of a mutation."""

    def _decorate(
        handler: Callable[[web.Request, str, JobSDK], Awaitable[web.StreamResponse]],
    ) -> Callable[[web.Request, str, JobSDK], Awaitable[web.StreamResponse]]:
        @wraps(handler)
        async def _wrapped(request: web.Request, app_name: str, sdk: JobSDK) -> web.StreamResponse:
            state = request.app.get("state")
            if state is not None:
                from kiro_crew.dashboard.handlers._shared import _is_restricted_session

                if _is_restricted_session(state, request):
                    _audit(app_name, operation, request.path, "denied", error="restricted session")
                    return web.json_response(
                        {
                            "error": "this session may not start or stop work",
                            "code": "restricted_session",
                        },
                        status=403,
                    )
            response = await handler(request, app_name, sdk)
            _audit(
                app_name,
                operation,
                request.path,
                "success" if response.status < 400 else "refused",
            )
            return response

        return _wrapped

    return _decorate


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is an empty one
        return {}
    return data if isinstance(data, dict) else {}


# ── Handlers ──


async def _handle_start(request: web.Request, app_name: str, sdk: JobSDK) -> web.StreamResponse:
    kind = request.match_info.get("kind", "")
    body = await _body(request)
    dedupe_key = body.get("dedupe_key", "")
    if not isinstance(dedupe_key, str):
        return web.json_response(
            {"error": "dedupe_key must be a string", "code": "invalid_dedupe_key"},
            status=400,
        )
    try:
        run_id = await sdk.start_async(kind, dedupe_key=dedupe_key)
    except UnknownJobKind as exc:
        # Not 400: the request is well-formed, the named kind simply does not
        # exist on this app -- and a kind with no runner must never queue a run
        # nothing will ever service.
        #
        # Scrubbed like every other outbound error here. The message quotes the
        # `kind` the CALLER chose, so an unregistered kind carrying a credential
        # was reflected into the response verbatim -- the one error path on this
        # surface that skipped `_safe`.
        return web.json_response({"error": _safe(str(exc)), "code": "unknown_job_kind"}, status=404)
    except JobError as exc:
        # The SDK refused to start: it could not persist the initial record, or
        # the host refused a thread. Without this the JobError would reach
        # aiohttp's default handler as a bare 500 carrying no machine-readable
        # code, which no client can switch on.
        return web.json_response({"error": _safe(str(exc)), "code": "job_start_failed"}, status=503)
    run, cancelling, live = await asyncio.to_thread(
        _read_with_snapshots, sdk, lambda: sdk.get(run_id)
    )
    if run is None:
        return web.json_response(
            {"error": "the run record could not be read back", "code": "run_unreadable"},
            status=500,
        )
    return web.json_response({"run": _public_view(run, cancelling, live)})


async def _handle_active(request: web.Request, app_name: str, sdk: JobSDK) -> web.StreamResponse:
    kind = request.query.get("kind", "")
    runs, cancelling, live = await asyncio.to_thread(
        _read_with_snapshots, sdk, lambda: sdk.list_active(kind)
    )
    return web.json_response({"runs": [_public_view(r, cancelling, live) for r in runs]})


async def _handle_recent(request: web.Request, app_name: str, sdk: JobSDK) -> web.StreamResponse:
    kind = request.query.get("kind", "")
    raw_limit = request.query.get("limit", "20")
    try:
        limit = int(raw_limit)
    except ValueError:
        return web.json_response(
            {"error": "limit must be an integer", "code": "invalid_limit"}, status=400
        )
    limit = max(1, min(limit, _RECENT_MAX))
    runs, cancelling, live = await asyncio.to_thread(
        _read_with_snapshots, sdk, lambda: sdk.list_recent(kind, limit)
    )
    return web.json_response({"runs": [_public_view(r, cancelling, live) for r in runs]})


async def _handle_get(request: web.Request, app_name: str, sdk: JobSDK) -> web.StreamResponse:
    run_id = request.match_info.get("run_id", "")
    run, cancelling, live = await asyncio.to_thread(
        _read_with_snapshots, sdk, lambda: sdk.get(run_id)
    )
    if run is None:
        return web.json_response({"error": "no such run", "code": "job_not_found"}, status=404)
    return web.json_response({"run": _public_view(run, cancelling, live)})


async def _handle_cancel(request: web.Request, app_name: str, sdk: JobSDK) -> web.StreamResponse:
    run_id = request.match_info.get("run_id", "")
    run, cancelling, live = await asyncio.to_thread(
        _read_with_snapshots, sdk, lambda: sdk.get(run_id)
    )
    if run is None:
        return web.json_response({"error": "no such run", "code": "job_not_found"}, status=404)
    accepted = await sdk.cancel_async(run_id)
    if not accepted:
        # Deliberately not an error: the run is finished, was never declared
        # cancellable, or belongs to a process that is gone. Saying so beats a
        # 200 that implies the run is stopping when nothing can reach it.
        return web.json_response(
            {
                "error": "this run cannot be cancelled",
                "code": "job_not_cancellable",
                "run": _public_view(run, cancelling, live),
            },
            status=409,
        )
    # Re-read AFTER the cancel, so the snapshot carries the request this call just
    # made. ``cancelling: True`` at the top level is what this CALL did; the
    # field inside ``run`` is what any later read of the record will now also
    # report, and the two legitimately differ once a fast worker has already
    # reached its checkpoint and recorded ``cancelled``.
    fresh, cancelling, live = await asyncio.to_thread(
        _read_with_snapshots, sdk, lambda: sdk.get(run_id)
    )
    return web.json_response(
        {"cancelling": True, "run": _public_view(fresh or run, cancelling, live)}
    )


# ── Registration ──


def register_job_routes(app: web.Application) -> None:
    """Mount the shared ``_jobs`` family. Call before the app catch-all.

    ``active`` and ``recent`` are registered BEFORE ``{run_id}``: aiohttp
    matches in registration order, so the literal segments would otherwise be
    captured as run ids and every list request would 404 as an unknown run.
    """
    r = app.router
    r.add_get(f"{_PREFIX}/active", _guarded(_handle_active))
    r.add_get(f"{_PREFIX}/recent", _guarded(_handle_recent))
    r.add_get(_PREFIX + "/{run_id}", _guarded(_handle_get))
    r.add_post(_PREFIX + "/{kind}/start", _guarded(_mutating("start")(_handle_start)))
    r.add_post(_PREFIX + "/{run_id}/cancel", _guarded(_mutating("cancel")(_handle_cancel)))
    logger.info("Job SDK routes registered under %s", _PREFIX)

"""Cloud provisioning API handlers — owner-only, user-initiated launch control.

Backs the ``/api/cloud/*`` routes that let the dashboard provision a Kiro Crew
instance in the user's own AWS account (the same flow as ``kirocrew cloud`` in a
terminal, driven as a durable background job — see :mod:`cloud.launch_job`).

Like the instances control plane, every route is **owner-only, never reachable
via Slack**, and emits a SEL audit event. Provisioning is a deliberate
**human/installer action** (the owner clicking Launch is the consent), which is
exactly the caller ``cloud.aws.assert_human_action`` permits — the gateway
process carries no ``KIROCREW_SESSION_KEY``, so the destructive verbs
(stop/start/destroy) are allowed here but blocked from any agent subprocess.

POSIX-only: the deploy engine shells to ``bash``/``aws``; Windows returns 400.
The exception is the two read-only launch-history routes, which parse a local
job store and shell to nothing — they answer on every platform so a Windows
dashboard can still render its Remote Crew list (see :func:`_guard`).
No new AWS logic lives here — it reuses the tested ``cloud/`` engine.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
import threading
from typing import TYPE_CHECKING, Optional

from aiohttp import web

from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam
from kiro_crew.cloud import launch_job as lj
from kiro_crew.cloud import source as source_mod
from kiro_crew.cloud import ssm
from kiro_crew.cloud.aws import AWSError, CloudActionDenied
from kiro_crew.cloud.launch_engine import RealLaunchEngine
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sel import sel
from kiro_crew.validation import ValidationError

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def _audit(operation: str, outcome: str, *, request_id: str = "", error: str = "") -> None:
    try:
        sel().log_tool_invocation(
            session_key="dashboard:cloud",
            tool_name=f"cloud_{operation}",
            outcome=outcome,
            request_id=request_id,
            source="dashboard",
            error=error,
        )
    except Exception:  # audit must never break the request path
        logger.debug("SEL audit failed for cloud_%s", operation, exc_info=True)


def _guard(
    request: web.Request, operation: str, *, posix_only: bool = True
) -> Optional[web.Response]:
    """Owner-only (non-Slack) + POSIX. Returns a denial Response or None.

    ``posix_only=False`` is for the two read-only launch-history routes, which
    only parse a local job store and shell to nothing. Failing those on Windows
    is not a harmless "unsupported" answer: the Remote Crew list deliberately
    waits for BOTH the instances and the launch history before rendering (a
    row's cloud-vs-manual identity decides what its delete button does), so a
    400 here replaced the whole list — including hand-added SSH crews that need
    no cloud provisioning at all — with the POSIX error. Reading the history
    reports whatever this host has persisted rather than guessing on the
    client's behalf: normally nothing after Windows-only use, since the write
    routes below stay POSIX-gated, but a config dir carried over from a POSIX
    host still answers with its real jobs, which is the honest reply.
    """
    if request.headers.get("X-Session-Key", "").startswith("slack:"):
        _audit(operation, "denied", error="slack-origin rejected")
        return web.json_response(
            {
                "error": "cloud provisioning is owner-only (not reachable via Slack)",
                "code": "cloud_owner_only",
            },
            status=403,
        )
    if not request.get("user"):
        _audit(operation, "denied", error="unauthenticated")
        return web.json_response(
            {
                "error": "authentication required (owner-only control plane)",
                "code": "auth_required",
            },
            status=401,
        )
    # Owner gate: delegated to the shared helper's predicate + stale relabel.
    if not is_owner_dashboard_request(request):
        _audit(operation, "denied", error="non-owner rejected")
        return _owner_denial_response(
            request,
            "cloud provisioning is owner-only (the dashboard owner, "
            "not an app or an allowed Slack user)",
            "cloud_owner_only",
        )
    if posix_only and sys.platform.startswith("win"):
        _audit(operation, "denied", error="windows unsupported")
        return web.json_response(
            {
                "error": "cloud provisioning requires a POSIX host (Linux/macOS); use WSL on Windows",
                "code": "posix_host_required",
            },
            status=400,
        )
    return None


def _store(state: "DashboardState") -> lj.LaunchJobStore:
    """The store object only — constructing it touches no disk."""
    store = getattr(state, "cloud_launch_store", None)
    if store is None:
        store = lj.LaunchJobStore()
        state.cloud_launch_store = store
    return store


async def _astore(state: "DashboardState") -> lj.LaunchJobStore:
    """The store, with the once-per-process orphan reap already done.

    The reap globs the job dir and rewrites what it finds, so it must not run on
    the event loop — the first cloud request after a restart would otherwise
    stall every other request and the heartbeat behind it. The flag is set before
    awaiting so a burst of concurrent requests triggers exactly one reap.
    """
    store = _store(state)
    if not getattr(state, "cloud_launch_reaped", False):
        state.cloud_launch_reaped = True
        try:
            await _in_executor(store.reap_orphans)
        except OSError as e:  # a read-only or missing store must not break the route
            logger.warning("Could not reap orphaned launch jobs: %s", e)
    return store


def _engine(state: "DashboardState") -> lj.LaunchEngine:
    # Tests inject a fake via ``state.cloud_launch_engine``.
    return getattr(state, "cloud_launch_engine", None) or RealLaunchEngine()


def _launch_lock(state: "DashboardState") -> LoopBoundLock:
    """Serializes the check-active → create → start-worker sequence.

    Without it the guard is check-then-act across an ``await``: two POSTs
    arriving together both see no active job, and each provisions its own
    CloudFormation stack — two billed instances the caller cannot undo.
    LoopBoundLock, not asyncio.Lock (#4800): the lock is cached on the
    long-lived DashboardState, which outlives any single event loop.
    """
    lock = getattr(state, "cloud_launch_lock", None)
    if lock is None:
        lock = LoopBoundLock()
        state.cloud_launch_lock = lock
    return lock


def _cancels(state: "DashboardState") -> dict:
    cancels = getattr(state, "cloud_launch_cancels", None)
    if cancels is None:
        cancels = {}
        state.cloud_launch_cancels = cancels
    return cancels


def _start_worker(state: "DashboardState", job: lj.LaunchJob) -> None:
    """Run the launch on a daemon thread (or inline when ``cloud_launch_sync``)."""
    store = _store(state)
    engine = _engine(state)
    cancel = threading.Event()
    _cancels(state)[job.id] = cancel
    # Claim the job for this process, so a later reap_orphans() does not mistake
    # a launch we are actively driving for one abandoned by a restart.
    store.adopt(job.id)

    def _run() -> None:
        try:
            lj.run_launch(job, store, engine, cancel=cancel)
        finally:
            _cancels(state).pop(job.id, None)

    if getattr(state, "cloud_launch_sync", False):
        _run()  # deterministic path for tests
    else:
        threading.Thread(target=_run, name=f"cloud-launch-{job.id}", daemon=True).start()


async def _in_executor(func, *args):
    """Run a blocking AWS call off the event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, func, *args)


# ── read endpoints ───────────────────────────────────────────────────────


async def api_cloud_preflight(request: web.Request) -> web.Response:
    """GET /api/cloud/preflight — doctor-as-JSON for the Set-up tab checklist."""
    denied = _guard(request, "preflight")
    if denied is not None:
        return denied
    profile = request.query.get("profile", "")
    region = request.query.get("region", "")
    reach = await _in_executor(iam.reachability_check, profile, region)
    plugin = await _in_executor(ssm.session_manager_plugin_installed)
    # The remedy is resolved server-side: this process knows which OS the check ran
    # on, and the browser does not (a remote gateway can be Linux while the user is
    # on a Mac). Empty when the platform has no one-liner — the UI then shows only
    # the localized "not installed" line.
    plugin_cmd = "" if plugin else await _in_executor(ssm.session_manager_plugin_install_command)
    _audit("preflight", "success")
    return web.json_response(
        {**reach, "session_manager_plugin": bool(plugin), "session_manager_plugin_command": plugin_cmd}
    )


async def api_cloud_iam_policy(request: web.Request) -> web.Response:
    """GET /api/cloud/iam-policy — the least-privilege policy JSON to attach."""
    denied = _guard(request, "iam_policy")
    if denied is not None:
        return denied
    _audit("iam_policy", "success")
    return web.json_response({"policy": iam.policy_json()})


async def api_cloud_launch_list(request: web.Request) -> web.Response:
    """GET /api/cloud/launch — all launch jobs (newest first)."""
    denied = _guard(request, "launch_list", posix_only=False)
    if denied is not None:
        return denied
    # list() globs the job dir and parses every file: cheap for a handful, but it
    # grows with history and would stall the whole gateway on the event loop.
    store = await _astore(request.app["state"])
    jobs = await _in_executor(store.list)
    _audit("launch_list", "success")
    return web.json_response({"jobs": [j.to_dict() for j in jobs]})


async def api_cloud_launch_get(request: web.Request) -> web.Response:
    """GET /api/cloud/launch/{id} — one job's live state (progress + sign-in)."""
    denied = _guard(request, "launch_get", posix_only=False)
    if denied is not None:
        return denied
    store = await _astore(request.app["state"])
    job = await _in_executor(store.get, request.match_info["id"])
    if job is None:
        return web.json_response({"error": "not found", "code": "launch_job_not_found"}, status=404)
    _audit("launch_get", "success", request_id=job.id)
    return web.json_response(job.to_dict())


# ── write endpoints ──────────────────────────────────────────────────────


async def api_cloud_launch_create(request: web.Request) -> web.Response:
    """POST /api/cloud/launch — start a launch job. Body: {profile, region, size_key}."""
    denied = _guard(request, "launch_create")
    if denied is not None:
        return denied
    state: "DashboardState" = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "invalid_body"}, status=400
        )
    size_key = str(body.get("size_key") or "").strip()
    # One launch at a time. Without this a double-click or a retried request
    # creates two jobs with two tags and two CloudFormation stacks — two billed
    # instances, and the client cannot undo that after the fact. The check, the
    # create and the worker start are held under one lock because the check
    # itself awaits: two POSTs arriving together would otherwise both pass it.
    async with _launch_lock(state):
        store = await _astore(state)
        existing = await _in_executor(store.list)
        active = next((j for j in existing if not j.terminal), None)
        if active is not None:
            _audit("launch_create", "denied", request_id=active.id, error="already running")
            return web.json_response(
                {
                    "error": "a crew setup is already running; cancel it before starting another",
                    "code": "launch_already_running",
                    "job": active.to_dict(),
                },
                status=409,
            )
        try:
            # create() does mkdir + a temp-write + os.replace; keep it off the event
            # loop like every other store call here (see _astore), so a slow disk
            # can't stall the gateway's other requests and its heartbeat behind it.
            job = await _in_executor(
                functools.partial(
                    store.create,
                    profile=str(body.get("profile", "")),
                    region=str(body.get("region", "")),
                    size_key=size_key,
                )
            )
        except KeyError as e:  # unknown size
            _audit("launch_create", "denied", error=str(e))
            return web.json_response(
                {"error": str(e).strip("'\""), "code": "invalid_launch_request"}, status=400
            )
        _start_worker(state, job)
    _audit("launch_create", "success", request_id=job.id)
    return web.json_response(job.to_dict(), status=202)


async def api_cloud_launch_cancel(request: web.Request) -> web.Response:
    """POST /api/cloud/launch/{id}/cancel — request cancellation of a running job."""
    denied = _guard(request, "launch_cancel")
    if denied is not None:
        return denied
    state: "DashboardState" = request.app["state"]
    job_id = request.match_info["id"]
    store = await _astore(state)
    job = await _in_executor(store.get, job_id)
    if job is None:
        return web.json_response({"error": "not found", "code": "launch_job_not_found"}, status=404)
    ev = _cancels(state).get(job_id)
    if ev is not None:
        ev.set()
    else:
        # No worker in this process owns a job the file still calls active, so setting
        # an event would cancel nothing while we answered 200. That is the "cancel
        # silently lies" case: terminalize it here instead.
        #
        # Re-read first. The snapshot above was taken across an await, and a worker
        # finishing in that gap saves its result and THEN pops its cancel event — so
        # arriving here does not prove the job is still active. Writing the stale
        # snapshot would overwrite a completed launch with `cancelled` and discard what
        # the worker recorded, including the instance id the dashboard uses to tell a
        # cloud crew from a hand-added machine.
        fresh = await _in_executor(store.get, job_id) or job
        if not fresh.terminal:
            for step in fresh.steps:
                if step.state == lj.STEP_ACTIVE:
                    step.state = lj.STEP_FAILED
            fresh.status = lj.CANCELLED
            fresh.signin = None
            fresh.error = "Cancelled — no setup was running for this job on this gateway."
            await _in_executor(store.save, fresh)
    _audit("launch_cancel", "success", request_id=job_id)
    updated = await _in_executor(store.get, job_id) or job
    return web.json_response(updated.to_dict())


async def api_cloud_launch_signin(request: web.Request) -> web.Response:
    """POST /api/cloud/launch/{id}/signin — fetch the pending device-code prompt.

    The job auto-polls for approval; this returns the URL + code to display (and
    409 when no sign-in is pending), so the UI has a dedicated fetch for it.
    """
    denied = _guard(request, "launch_signin")
    if denied is not None:
        return denied
    store = await _astore(request.app["state"])
    job = await _in_executor(store.get, request.match_info["id"])
    if job is None:
        return web.json_response({"error": "not found", "code": "launch_job_not_found"}, status=404)
    if job.status != lj.AWAITING_SIGNIN or not job.signin:
        return web.json_response(
            {"error": "no sign-in pending", "code": "no_signin_pending"}, status=409
        )
    _audit("launch_signin", "success", request_id=job.id)
    return web.json_response({"signin": job.signin.to_dict()})


def _teardown_after_delete(tag: str, profile: str, region: str, instance_id: str) -> None:
    """Drop local state for *tag*, but only once AWS confirms the stack is gone.

    Mirrors the CLI's destroy ordering (``cli_cloud.py``): confirm first, then
    unregister the instance and remove the uploaded source. If deletion does NOT
    confirm (``DELETE_FAILED``, or a gateway restart cutting this thread short),
    both are deliberately left in place — a crew that still exists must keep its
    registration, and the archive is the cheaper thing to leak. The opposite
    ordering loses the registration for a live instance, which the user cannot
    recover from the dashboard.
    """
    try:
        if not ec2.wait_for_delete(tag, profile, region):
            logger.warning(
                "Stack %s did not confirm deletion; leaving its registration and "
                "uploaded source in place.",
                tag,
            )
            return
    except AWSError as e:
        logger.warning("Could not confirm deletion of %s: %s", tag, e)
        return

    if instance_id:
        try:
            connect_mod.unregister_instance(instance_id)
        except Exception as e:  # never let local bookkeeping raise on a worker
            logger.warning("Could not unregister %s after destroy: %s", instance_id, e)
    try:
        source_mod.delete_source(tag, profile, region)
    except Exception as e:  # pragma: no cover - defensive, same as the CLI
        logger.warning("Could not remove the uploaded source for %s: %s", tag, e)


def _start_teardown_watch(
    tag: str, profile: str, region: str, instance_id: str, *, sync: bool = False
) -> None:
    """Run :func:`_teardown_after_delete` off the request (inline when *sync*)."""
    if sync:
        _teardown_after_delete(tag, profile, region, instance_id)
        return
    threading.Thread(
        target=_teardown_after_delete,
        args=(tag, profile, region, instance_id),
        name=f"cloud-teardown-{tag}",
        daemon=True,
    ).start()


async def _mutate_instance(request: web.Request, op: str) -> web.Response:
    """Shared stop/start/destroy: resolve the tag + profile/region, run off-loop."""
    denied = _guard(request, op)
    if denied is not None:
        return denied
    tag = request.match_info["tag"]
    profile = request.query.get("profile", "")
    region = request.query.get("region", "")
    # NB: no instance_id is read from the query. The client still sends one, but the
    # server derives it from the stack instead of trusting it — see _work() below.
    state: "DashboardState" = request.app["state"]
    store = _store(state)  # constructing it touches no disk
    sync_teardown = bool(getattr(state, "cloud_launch_sync", False))

    def _work() -> dict:
        if op == "stop":
            return ec2.stop(tag, profile, region)
        if op == "start":
            return ec2.start(tag, profile, region)
        # The instance id drives the registry cleanup below, and `unregister_instance`
        # matches it against EVERY registered box (by ssm_target, ssh_host or id) with
        # no cross-check against this tag. Accepting it from the caller therefore lets a
        # mismatched value silently remove a *different*, still-living crew's
        # registration — the exact harm `_teardown_after_delete` documents it exists to
        # prevent, and not recoverable from the dashboard. The server can derive it
        # authoritatively, so it always does: from the stack itself, and BEFORE the
        # delete, because the outputs are unreadable once the stack is gone.
        iid = ""
        try:
            iid = str(ec2.describe(tag, profile, region).get("instance_id") or "")
        except Exception as e:
            # Deliberately broad, and deliberately NOT falling back to a caller-supplied
            # id: an empty id skips the unregister, leaving a stale registry row the user
            # can see and remove. That is the safe direction to fail — the alternative
            # risks dropping the registration of a crew that is still running.
            # AWSError alone is not enough: describe shells out, so an exec/sandbox
            # failure surfaces as an unrelated exception type.
            logger.warning("Could not resolve the instance id for %s: %s", tag, e)
        if not iid:
            # `describe` cannot answer once the stack is gone — which is exactly the
            # retry case after a teardown was cut short (a restart kills the watcher
            # thread mid-wait). Without this the retry deletes an already-deleted stack
            # as a no-op, resolves no id, and skips the unregister AGAIN, so the row can
            # never be cleared from this panel. The launch job that created this tag
            # persists its instance id: still server-owned state, never caller input.
            iid = next(
                (j.instance_id for j in store.list() if j.tag == tag and j.instance_id), ""
            )
        # destroy: issue the delete and return; do not block the request on
        # DELETE_COMPLETE (minutes). A later status / the reaper reflects it.
        out = ec2.destroy(tag, profile, region, wait=False)
        # Local teardown (registry entry + uploaded source) mirrors the CLI's
        # destroy path, but it must NOT happen here: the delete is only *accepted*
        # at this point, and a stack that later reaches DELETE_FAILED would leave
        # a live crew whose registration and source archive we had already thrown
        # away. The CLI cleans up only after deletion confirms, so this waits for
        # the same confirmation on a background thread and cleans up then.
        _start_teardown_watch(tag, profile, region, iid, sync=sync_teardown)
        out["cleanup"] = "pending"
        return out

    try:
        result = await _in_executor(_work)
    except ValidationError as e:
        # ec2.* validates tag/profile/region and raises this — NOT an AWSError, so
        # without this arm a malformed tag in the URL path becomes a 500 instead of
        # telling the caller what was wrong with their input.
        _audit(op, "denied", request_id=tag, error=str(e))
        return web.json_response(
            {"error": str(e), "code": "invalid_cloud_parameter"}, status=400
        )
    except CloudActionDenied as e:
        _audit(op, "denied", request_id=tag, error=str(e))
        return web.json_response({"error": str(e), "code": "cloud_action_denied"}, status=403)
    except AWSError as e:
        _audit(op, "failure", request_id=tag, error=str(e))
        return web.json_response({"error": str(e), "code": "aws_call_failed"}, status=502)
    _audit(op, "success", request_id=tag)
    return web.json_response(result)


async def api_cloud_stop(request: web.Request) -> web.Response:
    """POST /api/cloud/{tag}/stop — stop the instance (pause compute billing)."""
    return await _mutate_instance(request, "stop")


async def api_cloud_start(request: web.Request) -> web.Response:
    """POST /api/cloud/{tag}/start — start a stopped instance."""
    return await _mutate_instance(request, "start")


async def api_cloud_destroy(request: web.Request) -> web.Response:
    """DELETE /api/cloud/{tag} — delete the stack (remove everything from AWS)."""
    return await _mutate_instance(request, "destroy")

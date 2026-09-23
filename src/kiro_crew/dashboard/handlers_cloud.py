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
import inspect
import logging
import sys
import threading
import time
from typing import TYPE_CHECKING, Mapping, Optional

from aiohttp import web

from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam
from kiro_crew.cloud import launch_job as lj
from kiro_crew.cloud import login as login_mod
from kiro_crew.cloud import source as source_mod
from kiro_crew.cloud import ssm
from kiro_crew.cloud.aws import AWSError, CloudActionDenied
from kiro_crew.cloud.launch_engine import RealLaunchEngine
from kiro_crew.cloud.login_target import KiroLoginTarget, LoginTargetError, target_from_whoami
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.sessions import fetch_local_identity
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.platform.interfaces import BUILTIN_PROVISIONER_ID
from kiro_crew.sandbox import SandboxCeilingUnsealable
from kiro_crew.sel import sel
from kiro_crew.validation import ValidationError

if TYPE_CHECKING:
    from kiro_crew.cloud.fargate_engine import TaskSighting
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


def _provisioners() -> list:
    """The deployment's provisioners, from the CPP ``remote_provisioners`` seam.

    ``safe_context_call`` fallback is the built-in descriptor alone: a degraded
    seam read keeps today's AWS tab working rather than emptying it, which is
    the fail-safe direction for a listing (nothing here mints or bills). Rows
    with a blank id or kind are dropped, never shadowed. Synchronous; called via
    ``_in_executor`` from a handler because context resolution can touch disk.
    """
    from kiro_crew.platform.context import current_context, safe_context_call
    from kiro_crew.platform.defaults import BUILTIN_REMOTE_PROVISIONER

    rows = safe_context_call(
        lambda: list(current_context().remote_provisioners.provisioners()),
        fallback_factory=lambda: [BUILTIN_REMOTE_PROVISIONER],
        log_message="remote_provisioners.provisioners degraded; offering the built-in lane only",
    )
    return [r for r in rows if getattr(r, "id", "") and getattr(r, "kind", "")]


def _provisioner_dict(p) -> dict:
    labels = dict(getattr(p, "step_labels", ()) or ())
    return {
        "id": p.id,
        "kind": p.kind,
        "label": getattr(p, "label", "") or p.id,
        "posix_only": bool(getattr(p, "posix_only", True)),
        "steps": [s.to_dict() for s in lj.default_steps(labels)],
        # Present on every row, empty for a lane with nothing to confirm, so a client reads
        # one shape rather than branching on whether the key exists.
        "confirm_before_launch": str(getattr(p, "confirm_before_launch", "") or ""),
    }


class LaunchUnavailable(Exception):
    """This lane cannot be launched right now, and the reason is the OPERATOR's to act on.

    Distinct from ``KeyError``, which means the id does not exist. This means the id exists and
    something about the host or the deployment stops it: a configuration reachable under a
    second name, or a provisioner that cannot receive the confirmation its own row demands.
    Both were reaching the client as HTTP 500 -- a bug report shape -- for conditions with a
    remedy the operator can carry out. ``code`` is the machine-readable tag the response
    carries so a client can branch without parsing prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _call_engine_for(provider, provisioner_id: str, confirmed_recipient: str):
    """Ask *provider* for an engine, passing the confirmation only if it can receive one.

    The seam's ``engine_for`` takes ``confirmed_recipient`` keyword-only WITH a default, so a
    provider written against the older signature is still conforming -- and calling it with the
    keyword raises ``TypeError``, which is not a launch failure any client can act on. The
    signature decides, rather than a try/except around the call: an exception-driven retry
    cannot tell a binding error from a ``TypeError`` raised inside the provider's own body, and
    retrying the latter would run half of it twice.

    A provider that cannot receive the value is called with the id alone, exactly as before the
    keyword existed. But if its row DECLARED a recipient to confirm, the launch is refused
    instead: a lane that publishes a credential recipient and then cannot be handed the
    operator's answer to it is the silent-substitution shape the confirmation exists to remove,
    and accepting the launch would leave the operator believing they had confirmed something.
    """
    accepts = False
    # Declared once, so the two branches share one type rather than the except branch having
    # to fit the shape `.parameters` happens to return (a MappingProxyType).
    params: Mapping[str, inspect.Parameter]
    try:
        params = inspect.signature(provider.engine_for).parameters
    except (TypeError, ValueError):
        # Not introspectable (a C callable, a wrapper with no signature). Treated as NOT
        # accepting, which is the conservative direction: the call still works, and a lane
        # that needs the confirmation is refused below rather than launched without it.
        params = {}
    if "confirmed_recipient" in params:
        accepts = True
    elif any(q.kind is inspect.Parameter.VAR_KEYWORD for q in params.values()):
        accepts = True

    if accepts:
        return provider.engine_for(provisioner_id, confirmed_recipient=confirmed_recipient)
    if confirmed_recipient:
        raise LaunchUnavailable(
            "lane_cannot_confirm",
            f"provisioner {provisioner_id!r} publishes a credential recipient to confirm, but "
            "its provider cannot accept the confirmation (its engine_for takes no "
            "confirmed_recipient). Nothing was launched",
        )
    return provider.engine_for(provisioner_id)


def _engine(
    state: "DashboardState",
    provider_id: str = BUILTIN_PROVISIONER_ID,
    confirmed_recipient: str = "",
) -> lj.LaunchEngine:
    """The engine that drives *provider_id*, from the CPP ``remote_provisioners`` seam.

    Tests inject a fake via ``state.cloud_launch_engine``; that hook outranks the
    seam so the existing launch-job tests keep exercising the orchestrator
    without composing a context. Raises ``KeyError`` for an id the provider does
    not know (the caller answers 400); a degraded seam read falls back to the
    built-in engine for the built-in id only, never to a guessed engine for an
    edition's id.

    ``confirmed_recipient`` is the credential recipient the operator confirmed in this
    request, forwarded to the seam untouched (see
    ``platform.interfaces.RemoteProvisionerProvider.engine_for``). It is POSITIONAL here
    rather than keyword-only so ``functools.partial`` in the handler stays one call; the
    seam takes it keyword-only.
    """
    injected = getattr(state, "cloud_launch_engine", None)
    if injected is not None:
        return injected
    from kiro_crew.platform.context import current_context, safe_context_call

    provider = safe_context_call(
        lambda: current_context().remote_provisioners,
        fallback_factory=lambda: None,
        log_message="remote_provisioners seam degraded; only the built-in engine is reachable",
    )
    if provider is None:
        if provider_id != BUILTIN_PROVISIONER_ID:
            raise KeyError(provider_id)
        return RealLaunchEngine()
    try:
        return _call_engine_for(provider, provider_id, confirmed_recipient)
    except SandboxCeilingUnsealable as exc:
        # The strict no-alias refusal on ``cloud.json``, raised where a launch consumes the
        # file. It is a real refusal and stays, but it describes a HOST setup an operator
        # chose -- a dotfile manager or a hardlinking backup leaves exactly this shape -- so it
        # belongs to them to fix, with the file and the remedy named. Uncaught it became a 500,
        # which reads as "the gateway is broken" for a condition one command clears.
        raise LaunchUnavailable("config_alias_refused", str(exc)) from exc


def _launch_lock(state: "DashboardState") -> LoopBoundLock:
    """Serializes the check-active → create → start-worker sequence.

    Without it the guard is check-then-act across an ``await``: two POSTs
    arriving together both see no active job, and each provisions its own
    CloudFormation stack — two billed instances the caller cannot undo.
    LoopBoundLock, not asyncio.Lock: the lock is cached on the
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


#: Serialises the register/release pair on ``_cancels``. Registration happens on
#: the event loop and release on a worker thread, so a compare-then-delete
#: without it could still read one worker's event and delete the next one's.
_CANCELS_GUARD = threading.Lock()


def _register_cancel(state: "DashboardState", job_id: str, cancel: threading.Event) -> None:
    with _CANCELS_GUARD:
        _cancels(state)[job_id] = cancel


def _release_cancel(state: "DashboardState", job_id: str, cancel: threading.Event) -> None:
    """Drop the registered event for *job_id* only if it is still *cancel*.

    A worker releases its event AFTER persisting its terminal result -- and the
    restart route admits a new retry for the same job as soon as the file reads
    terminal, registering a new event under the same id. An unconditional pop in
    the finishing worker's ``finally`` therefore raced that registration and could
    delete the NEW worker's event: a cancel then found nothing to set, wrote
    CANCELLED, and the new worker overwrote it with its result -- the cancel was
    silently ignored. Releasing only our own event closes that.
    """
    with _CANCELS_GUARD:
        cancels = _cancels(state)
        if cancels.get(job_id) is cancel:
            del cancels[job_id]


def _start_worker(
    state: "DashboardState", job: lj.LaunchJob, engine: Optional[lj.LaunchEngine] = None
) -> None:
    """Run the launch on a daemon thread (or inline when ``cloud_launch_sync``).

    ``engine`` is the one already resolved for ``job.provider_id``; resolving it
    again here would be a second seam read that could disagree with the first.
    """
    store = _store(state)
    if engine is None:
        engine = _engine(state, job.provider_id)
    cancel = threading.Event()
    _register_cancel(state, job.id, cancel)
    # Claim the job for this process, so a later reap_orphans() does not mistake
    # a launch we are actively driving for one abandoned by a restart.
    store.adopt(job.id)

    def _run() -> None:
        try:
            lj.run_launch(job, store, engine, cancel=cancel)
        finally:
            _release_cancel(state, job.id, cancel)

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
        {
            **reach,
            "session_manager_plugin": bool(plugin),
            "session_manager_plugin_command": plugin_cmd,
        }
    )


async def api_cloud_iam_policy(request: web.Request) -> web.Response:
    """GET /api/cloud/iam-policy — the least-privilege policy JSON to attach."""
    denied = _guard(request, "iam_policy")
    if denied is not None:
        return denied
    _audit("iam_policy", "success")
    return web.json_response({"policy": iam.policy_json()})


async def api_cloud_identity(request: web.Request) -> web.Response:
    """GET /api/cloud/identity — the launching machine's own Kiro sign-in.

    What the Remote Crew launch form preselects: an Identity Center user gets
    their organization's start URL as the crew's default sign-in target instead
    of the Builder ID portal. Owner-only like every cloud route (it names the
    operator's account type and SSO portal). Reuses the dashboard's existing
    sandbox-tiered ``kiro-cli whoami --format json`` fetch; the result is a
    SUGGESTION the form can override — never a credential, and never the
    launch's authority (that is the validated body of ``POST /api/cloud/launch``).

    ``{"identity": {account_type?, start_url?}, "suggested_target": {...},
    "discovery": "read"}`` when whoami answered (an empty identity is a real
    answer: this machine has no sign-in to inherit, and the default target is
    the suggestion). ``{"identity": null, "suggested_target": null,
    "discovery": "unknown"}`` when it could not answer -- timed out, failed to
    start, exited with an error -- and ``{"identity": {...}, "suggested_target":
    null, "discovery": "unknown"}`` when it named Identity Center without a
    readable start URL (which organization is unknown), so the form does not
    present the Builder ID default as though it had been read; the user chooses
    by hand. The Identity
    Center REGION is not something whoami reports, so the suggested target
    carries it empty for the form to complete.
    """
    denied = _guard(request, "identity", posix_only=False)
    if denied is not None:
        return denied

    identity: dict[str, object] | None
    try:
        identity = await fetch_local_identity()
    except Exception:  # noqa: BLE001 - discovery is advisory
        logger.debug("cloud identity discovery failed", exc_info=True)
        identity = None
    if identity is None:
        _audit("identity", "success")
        return web.json_response(
            {"identity": None, "suggested_target": None, "discovery": "unknown"}
        )
    public = {k: v for k, v in identity.items() if k in ("account_type", "start_url")}
    suggested = target_from_whoami(identity)
    if suggested is None:
        # Identity Center, but WHICH organization is not readable: suggesting the
        # Builder ID default here would be the silent downgrade; the form asks.
        _audit("identity", "success")
        return web.json_response(
            {"identity": public, "suggested_target": None, "discovery": "unknown"}
        )
    _audit("identity", "success")
    return web.json_response(
        {"identity": public, "suggested_target": suggested.to_dict(), "discovery": "read"}
    )


async def api_cloud_provisioners(request: web.Request) -> web.Response:
    """GET /api/cloud/provisioners — the lanes the Set-up tab may offer.

    Descriptor-only (``{id, kind, label, posix_only, steps}``): which provisioners
    exist and how to draw them, never how to run one. The list is presentation:
    ``POST /api/cloud/launch`` re-resolves the requested id against the same seam
    before persisting a job. Answers on every platform (``posix_only=False``),
    like the launch-history routes: the tab needs the list to decide WHICH form
    to draw, and a provisioner that does not shell to ``aws`` may well run on a
    Windows gateway; the per-descriptor ``posix_only`` flag carries that answer.
    """
    denied = _guard(request, "provisioners", posix_only=False)
    if denied is not None:
        return denied
    rows = await _in_executor(_provisioners)
    _audit("provisioners", "success")
    return web.json_response({"provisioners": [_provisioner_dict(p) for p in rows]})


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


def _task_dict(sighting: "TaskSighting") -> dict:
    """The wire shape of one task sighting, cluster and id split out of the ARN
    so the client never parses an ARN itself."""
    # Deferred like platform/defaults.py defers it: the Fargate module pulls the
    # whole fargate package in, and this handler module is imported by every
    # dashboard boot, Fargate lane configured or not.
    from kiro_crew.cloud.fargate_engine import split_task_arn

    cluster, task_id = split_task_arn(sighting.task_arn)
    # `started_by` and `tags` stay on the sighting for the engine's ownership
    # rule; the panel has no reader for them, so they do not cross the wire.
    return {
        "task_arn": sighting.task_arn,
        "cluster": cluster,
        "task_id": task_id,
        "last_status": sighting.last_status,
        "desired_status": sighting.desired_status,
        "started_at": sighting.started_at,
        "stopped_at": sighting.stopped_at,
        "stopped_reason": sighting.stopped_reason,
    }


async def api_cloud_launch_task(request: web.Request) -> web.Response:
    """GET /api/cloud/launch/{id}/task — the task a launch started, read from ECS now.

    The cloud panel's read for a Fargate launch. The EC2 lane's "is it still
    there" is answered by the Instances registry, which a teardown updates. A
    Fargate crew's record addresses ONE task, so it names the task a launch
    started and cannot report that task's current state; ECS itself is the only
    source that can, and this route is the panel's one path to it. It is read-only
    and shells to ``aws ecs describe-tasks`` for exactly the ARN the job recorded,
    so it is POSIX-gated like every other route here that runs the AWS CLI.

    Answers ``{"job_id", "task_arn", "read_at", "task"}``. ``task`` is the
    sighting, or ``null`` when ECS does not list the ARN (ECS drops a stopped
    task after about an hour); the client says exactly that and nothing more.
    ``read_at`` is when THIS read happened, so the client can show a status as a
    reading at an instant rather than as a standing fact.

    Refusals name their cause so the client renders "could not read", never a
    state: ``launch_job_not_found`` (404), ``launch_task_not_recorded`` (the
    launch never got as far as starting a task), ``unknown_provisioner`` (the
    lane that ran it is not configured here), ``provisioner_cannot_describe``
    (the lane's engine has no single-task read: the EC2 lane, or an edition's),
    ``aws_call_failed`` (502).
    """
    denied = _guard(request, "launch_task")
    if denied is not None:
        return denied
    state: "DashboardState" = request.app["state"]
    store = await _astore(state)
    job = await _in_executor(store.get, request.match_info["id"])
    if job is None:
        return web.json_response({"error": "not found", "code": "launch_job_not_found"}, status=404)
    if not job.instance_id:
        _audit("launch_task", "denied", request_id=job.id, error="no task recorded")
        return web.json_response(
            {"error": "this launch recorded no task to read", "code": "launch_task_not_recorded"},
            status=400,
        )
    try:
        # Off the loop, like the create path: resolving the Fargate lane reads
        # cloud.json (and checks it for aliases), and a slow filesystem would
        # otherwise stall every request and the heartbeat behind one panel open.
        engine = await _in_executor(functools.partial(_engine, state, job.provider_id))
    except KeyError:
        _audit("launch_task", "denied", request_id=job.id, error="no engine")
        return web.json_response(
            {
                "error": f"provisioner {job.provider_id!r} has no launch engine",
                "code": "unknown_provisioner",
            },
            status=400,
        )
    except LaunchUnavailable as exc:
        _audit("launch_task", "denied", request_id=job.id, error=f"{exc.code}: {exc}")
        return web.json_response({"error": str(exc), "code": exc.code}, status=400)
    describe = getattr(engine, "describe_task", None)
    if not callable(describe):
        # Capability, not identity: the EC2 engine has no single-task read
        # because its liveness lives in the registry, and an edition's lane may
        # or may not have one. Keying on the method rather than on a provider id
        # keeps this route honest for a lane this file has never heard of.
        _audit("launch_task", "denied", request_id=job.id, error="engine cannot describe a task")
        return web.json_response(
            {
                "error": f"provisioner {job.provider_id!r} does not read a task's status",
                "code": "provisioner_cannot_describe",
            },
            status=400,
        )
    try:
        sighting = await _in_executor(
            functools.partial(
                describe, task_arn=job.instance_id, profile=job.profile, region=job.region
            )
        )
    except (ValidationError, ValueError) as e:
        _audit("launch_task", "denied", request_id=job.id, error=str(e))
        return web.json_response({"error": str(e), "code": "invalid_cloud_parameter"}, status=400)
    except AWSError as e:
        _audit("launch_task", "failure", request_id=job.id, error=str(e))
        return web.json_response({"error": str(e), "code": "aws_call_failed"}, status=502)
    _audit("launch_task", "success", request_id=job.id)
    return web.json_response(
        {
            "job_id": job.id,
            "task_arn": job.instance_id,
            "read_at": time.time(),
            "task": None if sighting is None else _task_dict(sighting),
        }
    )


# ── write endpoints ──────────────────────────────────────────────────────


async def api_cloud_launch_create(request: web.Request) -> web.Response:
    """POST /api/cloud/launch — start a launch job.

    Body: ``{provider_id?, profile, region, size_key, login_target?, confirm_recipient?}``.
    ``confirm_recipient`` is the credential recipient the operator confirmed, required by any
    lane whose launch delivers a credential to something its own configuration names (the
    Fargate lane refuses an empty or stale one and names both values); the built-in EC2 lane
    makes no such choice and ignores it. ``provider_id`` names a
    row of ``GET /api/cloud/provisioners`` and defaults to the built-in EC2 lane,
    so a pre-seam client body launches exactly what it always did. ``login_target``
    is ``{license, start_url, region}`` — the Kiro identity the crew signs in as
    (``region`` here is the IAM Identity Center region, NOT the EC2 ``region``
    above); absent means Builder ID. The POSIX gate
    is per descriptor: the built-in shells to ``bash``/``aws`` and needs one, an
    edition's provisioner says for itself.
    """
    # POSIX is decided below, per provisioner, once the body names one.
    denied = _guard(request, "launch_create", posix_only=False)
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
    provider_id = str(body.get("provider_id") or BUILTIN_PROVISIONER_ID).strip()
    # The credential recipient the OPERATOR confirmed for this launch, verbatim. Passed to
    # the engine and compared there against what the launch would actually deliver to; a
    # lane that hands out no credential of its own ignores it. Not validated here: this
    # handler has no way to know what a lane's recipient looks like, and a check that
    # guessed would be a second rule to keep in step with the engine's.
    confirm_recipient = str(body.get("confirm_recipient") or "")
    # The Kiro identity the crew signs in as — validated HERE, at the owner-only
    # HTTP boundary, before anything is persisted or reaches a remote shell.
    # An absent block is the Builder ID default (a pre-seam client body launches
    # exactly what it always did); a present-but-invalid one is a coded 400,
    # never a silent fall-back to the wrong identity.
    raw_target = body.get("login_target")
    if raw_target is not None and not isinstance(raw_target, dict):
        _audit("launch_create", "denied", error="invalid login target: not an object")
        return web.json_response(
            {"error": "login_target must be an object", "code": "invalid_login_target"}, status=400
        )
    try:
        login_target = KiroLoginTarget.from_fields(
            license=str((raw_target or {}).get("license") or ""),
            start_url=str((raw_target or {}).get("start_url") or ""),
            region=str((raw_target or {}).get("region") or ""),
        )
    except LoginTargetError as e:
        _audit("launch_create", "denied", error=f"invalid login target: {e}")
        return web.json_response({"error": str(e), "code": "invalid_login_target"}, status=400)
    provisioner = next((p for p in await _in_executor(_provisioners) if p.id == provider_id), None)
    if provisioner is None:
        _audit("launch_create", "denied", error=f"unknown provisioner {provider_id!r}")
        return web.json_response(
            {
                "error": f"no provisioner {provider_id!r} on this deployment",
                "code": "unknown_provisioner",
            },
            status=400,
        )
    required = str(getattr(provisioner, "confirm_before_launch", "") or "")
    if required and confirm_recipient != required:
        # DERIVED from the row this request named, not hard-coded to one lane: a lane that
        # hands a credential to something its own configuration chooses says so on its
        # descriptor, and this is the same value `GET /api/cloud/provisioners` publishes. So a
        # client cannot reach a launch without having read what it must confirm, which is the
        # difference between the operator judging the recipient and echoing a string back.
        #
        # The engine refuses again with what it is ABOUT to launch, which is the check that
        # cannot be skipped by another caller; this one exists so the refusal arrives before a
        # job file is persisted, and it names the value so a stale client can correct itself.
        _audit("launch_create", "denied", error="credential recipient not confirmed")
        return web.json_response(
            {
                "error": (
                    f"this launch would hand the model credential to {required}; confirm that "
                    "recipient in the request (confirm_recipient) before launching"
                ),
                "code": "recipient_not_confirmed",
                "confirm_before_launch": required,
            },
            status=400,
        )
    if provisioner.posix_only and sys.platform.startswith("win"):
        _audit("launch_create", "denied", error="windows unsupported")
        return web.json_response(
            {
                "error": "cloud provisioning requires a POSIX host (Linux/macOS); use WSL on Windows",
                "code": "posix_host_required",
            },
            status=400,
        )
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
        # Resolve the engine BEFORE the job exists: an id the provider lists but
        # cannot back must not leave a PENDING job file that a restart then reaps
        # as "interrupted" for a launch that never started.
        try:
            # Off the loop like every other disk-touching call in this handler. The
            # built-in id resolves without reading anything, but the Fargate id reads
            # cloud.json through the seam, and a slow disk on that read would stall
            # the gateway's other requests and its heartbeat behind it -- the reason
            # the store calls above are wrapped.
            engine = await _in_executor(
                functools.partial(_engine, state, provider_id, confirm_recipient)
            )
        except KeyError:
            _audit("launch_create", "denied", error=f"no engine for {provider_id!r}")
            return web.json_response(
                {
                    "error": f"provisioner {provider_id!r} has no launch engine",
                    "code": "unknown_provisioner",
                },
                status=400,
            )
        except LaunchUnavailable as exc:
            # 400, not 500: the id exists and the request is well formed, but the deployment or
            # the host stops this launch and the message says what to change. A 500 would tell
            # the operator to file a bug over a symlink they created on purpose.
            _audit("launch_create", "denied", error=f"{exc.code}: {exc}")
            return web.json_response({"error": str(exc), "code": exc.code}, status=400)
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
                    provider_id=provider_id,
                    step_labels=dict(provisioner.step_labels or ()),
                    login_target=login_target,
                )
            )
        except KeyError as e:  # unknown size
            _audit("launch_create", "denied", error=str(e))
            return web.json_response(
                {"error": str(e).strip("'\""), "code": "invalid_launch_request"}, status=400
            )
        _start_worker(state, job, engine)
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
    # Under the launch lock: the restart route registers its cancel event and
    # persists RUNNING under this same lock, so a cancel cannot slip between those
    # two writes, find no event, and terminalize a job whose worker is about to
    # start. Without the lock that ordering was only probable, not guaranteed.
    async with _launch_lock(state):
        ev = _cancels(state).get(job_id)
        if ev is not None:
            ev.set()
        else:
            # No worker in this process owns a job the file still calls active, so
            # setting an event would cancel nothing while we answered 200. That is
            # the "cancel silently lies" case: terminalize it here instead.
            #
            # Re-read first. The snapshot above was taken across an await, and a
            # worker finishing in that gap saves its result and THEN pops its cancel
            # event — so arriving here does not prove the job is still active.
            # Writing the stale snapshot would overwrite a completed launch with
            # `cancelled` and discard what the worker recorded, including the
            # instance id the dashboard uses to tell a cloud crew from a hand-added
            # machine.
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


def _preserved_code_may_be_approved(job: lj.LaunchJob) -> bool:
    """Is this the one shape where the box can be signed in behind our back?

    A sign-in that ran out of time deliberately KEEPS its device code and leaves
    the remote login polling, so the user can still approve it from the browser
    tab that is already open. Nothing re-probes the instance after that, so an
    approval that lands then signs the crew in while the job still reads
    ``signin_detected=False`` — badged "Needs sign-in" forever, with Connect held.

    True only for: terminal (no worker owns it, so nothing else is writing it),
    registered with an instance to probe, not already signed in, and still
    holding the surviving prompt that makes the approval possible. Deliberately
    narrow — this must never become a blanket poll of every job, and never touch
    a job a worker is driving.
    """
    if not job.terminal or job.signin_detected or not job.instance_id or not job.signin:
        return False
    if lj.target_is_unreadable(job):
        # `from_dict` substituted the DEFAULT identity for one it could not parse,
        # so probing would ask "is this box signed in as Builder ID?" about a crew
        # that belongs to an org portal -- and a match would `mark_signed_in`,
        # release Connect and save the substitution over the original bytes. Same
        # refusal as the restart route (`api_cloud_launch_signin_restart`).
        return False
    try:
        return job.step(lj.STEP_CONNECT).state == lj.STEP_DONE
    except KeyError:  # a provisioner with different steps: nothing to recover
        return False


def _probe_signin_on_box(state: "DashboardState", job: lj.LaunchJob) -> Optional[bool]:
    """Query sign-in without writing, returning None if the instance cannot be checked.

    Runs in an executor because it is an SSM round trip of seconds. Deliberately
    free of mutation: a write here would run on that worker thread, where nothing
    serialises it against a concurrent restart persisting RUNNING under
    :func:`_launch_lock`. A re-read narrows that window rather than closing it, so
    :func:`_record_probed_signin` owns the write -- on the loop, under the lock.
    """
    try:
        return bool(
            login_mod.is_logged_in(
                job.instance_id, job.profile, job.region, target=job.login_target
            )
        )
    except Exception:  # noqa: BLE001 - a probe failure is not a launch failure
        logger.info("could not re-probe the Kiro sign-in for %s", job.id, exc_info=True)
        return None


async def _record_probed_signin(state: "DashboardState", job: lj.LaunchJob) -> bool:
    """Persist a confirmed out-of-band sign-in, under the launch lock.

    Same lock :func:`_claim_signin` writes under, which is what makes this safe: a
    restart admitted while the probe was talking to the box has already persisted
    RUNNING, and this must not put a stale terminal snapshot back over it. Holding
    the lock across the (fast, local) re-read and save is what serialises them --
    the SSM round trip stays outside it, in :func:`_probe_signin_on_box`.

    Returns whether the write happened. ``False`` means the job moved on and the
    probe result is stale, which is not an error: the newer writer wins.
    """
    store = await _astore(state)
    async with _launch_lock(state):
        fresh = await _in_executor(store.get, job.id)
        if fresh is None:
            return False
        if not _preserved_code_may_be_approved(fresh):
            logger.info("dropping a stale sign-in probe result for %s: the job moved on", job.id)
            return False
        lj.mark_signed_in(fresh)
        await _in_executor(store.save, fresh)
        # Keep the caller's object consistent with what was persisted.
        lj.mark_signed_in(job)
    return True


async def api_cloud_launch_signin(request: web.Request) -> web.Response:
    """POST /api/cloud/launch/{id}/signin — fetch the pending device-code prompt.

    The job auto-polls for approval; this returns the URL + code to display (and
    409 when no sign-in is pending), so the UI has a dedicated fetch for it.

    A terminal job that still holds a PRESERVED code is re-probed first: nothing
    else re-checks the box after the job goes terminal, so a code approved out of
    band left the crew signed in while the job said it was not. The probe runs
    only on that narrow shape (see :func:`_preserved_code_may_be_approved`) and
    answers ``signin_already_complete``, with the corrected job, instead of the
    bare "no sign-in pending" that hid it.
    """
    denied = _guard(request, "launch_signin")
    if denied is not None:
        return denied
    state: "DashboardState" = request.app["state"]
    store = await _astore(state)
    job = await _in_executor(store.get, request.match_info["id"])
    if job is None:
        return web.json_response({"error": "not found", "code": "launch_job_not_found"}, status=404)
    if job.status != lj.AWAITING_SIGNIN or not job.signin:
        if _preserved_code_may_be_approved(job):
            # Query off the loop, write on it under the lock. Both halves must
            # succeed to report the crew signed in: a probe that says yes but whose
            # write is dropped means another writer moved the job on, and the
            # caller must not claim a state that was not persisted.
            signed = await _in_executor(functools.partial(_probe_signin_on_box, state, job))
            if signed is None:
                error = "could not reach the crew to check its sign-in"
                _audit("launch_signin", "error", request_id=job.id, error=error)
                return web.json_response(
                    {"error": error, "code": "signin_probe_failed"}, status=502
                )
            if signed and await _record_probed_signin(state, job):
                _audit("launch_signin", "success", request_id=job.id)
                return web.json_response(
                    {
                        "error": "already signed in",
                        "code": "signin_already_complete",
                        "job": job.to_dict(),
                    },
                    status=409,
                )
        return web.json_response(
            {"error": "no sign-in pending", "code": "no_signin_pending"}, status=409
        )
    _audit("launch_signin", "success", request_id=job.id)
    return web.json_response({"signin": job.signin.to_dict()})


def _claim_signin(state: "DashboardState", job: lj.LaunchJob) -> None:
    """Persist the job as RUNNING before the lock that admitted it is released.

    :func:`lj.run_signin_retry` writes this transition too, but it runs on the
    worker — so between this handler returning and that first save, the job file
    still reads terminal. A second restart request in that window passed the
    "nothing active" check, and two remote logins raced with one cancel handle
    between them. The guard is only a guard if the state it reads is already
    written.
    """
    job.step(lj.STEP_SIGNIN).state = lj.STEP_ACTIVE
    job.status = lj.RUNNING
    # NOT `job.signin = None`: the previous device code stays until the worker
    # has replaced the login on the box (`run_signin_retry` clears it only after
    # `begin_signin` returns). The old poller is still live until then, and a
    # thread-start or SSM failure here must not leave it untracked.
    job.signin_detected = False
    job.error = ""
    store = _store(state)
    # Adopt BEFORE the save, as `_start_worker` does. `reap_orphans` skips a job
    # only when it is terminal or owned by this process, so a RUNNING retry that
    # is not yet adopted is exactly what a first-use reap -- fired by any other
    # cloud request arriving at startup -- reads as abandoned and parks back to
    # DONE. A second restart is then admitted, and two logins race with one
    # tracked device code between them. Adopting first closes the window: the
    # worker re-adopts (idempotent) when it starts.
    store.adopt(job.id)
    store.save(job)


def _start_signin_worker(
    state: "DashboardState",
    job: lj.LaunchJob,
    engine: lj.LaunchEngine,
    cancel: threading.Event,
) -> None:
    """Run :func:`lj.run_signin_retry` on a daemon thread (inline when ``cloud_launch_sync``).

    *cancel* is registered in ``_cancels`` by the CALLER, before the claim that
    persists RUNNING -- not here. Registering it here left a gap in which the
    file already said RUNNING but no event existed, so a cancel arriving then
    found nothing to set, terminalized the job CANCELLED, and the worker that
    started a moment later overwrote that with DONE.
    """
    store = _store(state)
    # Claim the job for this process, so a later reap_orphans() does not mistake
    # a sign-in we are actively driving for one abandoned by a restart.
    store.adopt(job.id)

    def _run() -> None:
        try:
            lj.run_signin_retry(job, store, engine, cancel=cancel)
        finally:
            # Only OUR event: the result was saved above, so a restart may already
            # have registered the next retry's event under this id.
            _release_cancel(state, job.id, cancel)

    if getattr(state, "cloud_launch_sync", False):
        _run()  # deterministic path for tests
        return
    try:
        threading.Thread(target=_run, name=f"cloud-signin-{job.id}", daemon=True).start()
    except RuntimeError:
        # `Thread.start` raises when the process is out of threads. Release only
        # the in-memory handle here; the on-disk revert is the caller's, because
        # this function is sync and store I/O belongs off the event loop
        # (see `_unclaim_signin`).
        _release_cancel(state, job.id, cancel)
        raise


def _unclaim_signin(state: "DashboardState", job: lj.LaunchJob) -> None:
    """Undo `_claim_signin` after the worker failed to start. Runs in an executor.

    The claim has ALREADY persisted RUNNING, and the only thing that ever moves
    a job off RUNNING is the worker that failed to exist. Left alone, every later
    launch and restart answers 409 launch_already_running -- permanently.
    """
    store = _store(state)
    fresh = store.get(job.id) or job
    # DONE with the error, not FAILED: the crew exists and is registered, and a
    # red card over a working crew is the shape the reap fix removes elsewhere.
    # DONE-unsigned keeps the row's "Needs sign-in" badge and its Start sign-in
    # affordance, which is exactly the retry the error asks for. Same shape as
    # `run_signin_retry`'s own failure arm.
    fresh.status = lj.DONE
    fresh.step(lj.STEP_SIGNIN).state = lj.STEP_SKIPPED
    fresh.error = "Could not start the sign-in worker (the gateway is out of threads). Try again."
    store.save(fresh)


async def api_cloud_launch_signin_restart(request: web.Request) -> web.Response:
    """POST /api/cloud/launch/{id}/signin/restart — start (again) the Kiro sign-in.

    The dashboard's "Start sign-in" / "Start over with a new code" action for a crew whose
    launch finished without a confirmed sign-in — the code timed out, nobody
    approved it, or the gateway restarted mid-wait. Re-runs **only** the sign-in
    step, with the identity stored on the job; the crew itself is left alone and
    nothing is re-provisioned. 409 while a launch or another sign-in is already
    running (the guard that keeps one device code live at a time), 400 when the
    job has no crew to sign in on.

    It cannot change WHICH identity signs in: the job's persisted
    ``login_target`` is reused, which is the point — an Identity Center crew must
    never silently get a Builder ID code. A crew launched against the wrong start
    URL is fixed by deleting it and launching again, since the identity is chosen
    before the instance exists.

    Owner-only and audited like every other cloud route.
    """
    denied = _guard(request, "launch_signin_restart")
    if denied is not None:
        return denied
    state: "DashboardState" = request.app["state"]
    job_id = request.match_info["id"]
    # Same lock as create: the "nothing active" check awaits, so two restarts
    # arriving together would otherwise both pass it and race two remote logins.
    async with _launch_lock(state):
        store = await _astore(state)
        job = await _in_executor(store.get, job_id)
        if job is None:
            return web.json_response(
                {"error": "not found", "code": "launch_job_not_found"}, status=404
            )
        if job.signin_detected:
            # A stale tab asking to restart a sign-in that another tab (or the
            # re-probe) has since confirmed. Admitting it would clear
            # `signin_detected` in the claim, and an SSM failure after that would
            # persist a signed-in crew as unsigned. Nothing to do: say so.
            _audit("launch_signin_restart", "denied", request_id=job_id, error="already signed in")
            return web.json_response(
                {
                    "error": "this crew is already signed in",
                    "code": "signin_already_complete",
                    "job": job.to_dict(),
                },
                status=409,
            )
        if lj.target_is_unreadable(job):
            # The job named an identity this release cannot parse, so `from_dict`
            # substituted the DEFAULT (Builder ID). Admitting the restart would
            # persist that substitution over the original bytes and start a
            # Builder ID device flow on a crew that belongs to an org portal --
            # the silent identity downgrade the target exists to prevent, with the
            # start URL gone from the job and nothing to recover it from. The
            # remedy is a release that can read the target, or deleting the crew
            # and launching again; not a sign-in from here.
            _audit(
                "launch_signin_restart", "denied", request_id=job_id, error="unreadable identity"
            )
            return web.json_response(
                {
                    "error": (
                        "this crew's Kiro identity cannot be read by this version, so a "
                        "sign-in here would use the wrong account"
                    ),
                    "code": "login_target_unreadable",
                    "job": job.to_dict(),
                },
                status=409,
            )
        if not job.instance_id or job.step(lj.STEP_CONNECT).state != lj.STEP_DONE:
            _audit("launch_signin_restart", "denied", request_id=job_id, error="no crew")
            return web.json_response(
                {
                    "error": "this setup did not create a crew to sign in on",
                    "code": "launch_has_no_instance",
                },
                status=400,
            )
        existing = await _in_executor(store.list)
        active = next((j for j in existing if not j.terminal), None)
        if active is not None:
            _audit("launch_signin_restart", "denied", request_id=job_id, error="already running")
            return web.json_response(
                {
                    "error": "a crew setup or sign-in is already running; wait for it or cancel it",
                    "code": "launch_already_running",
                    "job": active.to_dict(),
                },
                status=409,
            )
        try:
            engine = _engine(state, job.provider_id)
        except KeyError:
            _audit("launch_signin_restart", "denied", request_id=job_id, error="no engine")
            return web.json_response(
                {
                    "error": f"provisioner {job.provider_id!r} has no launch engine",
                    "code": "unknown_provisioner",
                },
                status=400,
            )
        # Event BEFORE claim, both under the lock: once RUNNING is on disk a
        # cancel must find something to set, or it terminalizes a job the worker
        # is about to drive (see `_start_signin_worker`).
        cancel = threading.Event()
        _register_cancel(state, job.id, cancel)
        await _in_executor(functools.partial(_claim_signin, state, job))
        try:
            _start_signin_worker(state, job, engine, cancel)
        except RuntimeError:
            # Revert the claim off the loop, still under the launch lock that made
            # it, then tell the caller the truth rather than 202-ing a sign-in that
            # is not running. Retryable, so 503.
            await _in_executor(functools.partial(_unclaim_signin, state, job))
            _audit("launch_signin_restart", "error", request_id=job_id, error="thread start failed")
            return web.json_response(
                {
                    "error": "could not start the sign-in worker; try again",
                    "code": "signin_worker_unavailable",
                },
                status=503,
            )
    _audit("launch_signin_restart", "success", request_id=job_id)
    updated = await _in_executor(store.get, job_id) or job
    return web.json_response(updated.to_dict(), status=202)


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
            iid = next((j.instance_id for j in store.list() if j.tag == tag and j.instance_id), "")
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
        return web.json_response({"error": str(e), "code": "invalid_cloud_parameter"}, status=400)
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

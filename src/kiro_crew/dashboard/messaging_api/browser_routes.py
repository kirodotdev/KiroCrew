"""The browser routes: the attach token, the native panel's command bus, the
Playwright CLI install and the browser view.

``PUT /api/browser/token``; ``POST /api/browser/command`` with its
``command-drain`` and ``command-result`` legs; ``GET``/``POST /api/browser/install``
and ``POST /api/browser/engine``; ``GET /api/browser/view`` with ``view/start``; and
``POST /api/browser/open``. Also the owner check every mutation runs and the install
job the gateway stops at shutdown (``stop_browser_install``).
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        DEFAULT_COMMAND_TIMEOUT_MS,
        DEFAULT_DRAIN_WAIT_MS,
        ROUTE_PREFIX,
        DashboardState,
        KiroCrewConfig,
        NoPanelError,
        QueueFullError,
        _sel,
        browser_cli_install,
        browser_cli_launcher,
        browser_cli_token,
        browser_cli_view,
        browser_install_job,
        get_command_bus,
        logger,
    )


def _deny_non_owner_browser_request(request: web.Request, operation: str) -> web.Response | None:
    """Require the dashboard owner on browser MUTATION endpoints. 403 or None.

    The caller must be the configured owner (``is_owner_dashboard_request``):
    app tokens, non-owner dashboard users, and callers with no identity are all
    refused. This is the same predicate used for MCP-app calls, source-provider
    mutations, and agent-question endpoints — one definition of "owner" across
    the dashboard.

    The mutations guarded here are security-sensitive:

    * installing the CLI globally adds an executable host capability, so a
      non-owner must not be able to mutate the machine to provide it;
    * the attach token is a stored credential that silences the browser's own
      per-attach approval prompt — the last human checkpoint before a program
      drives the user's logged-in session;
    * a browser download writes to the machine;
    * the view endpoints return an unauthenticated dashboard URL, i.e. control
      of a logged-in browser.

    Reads (``api_browser_install_get``) stay open: knowing whether browsing
    exists is not a capability. Mirrors the ComputerUse keystone precedent.
    """
    # Deferred import: source_providers imports chat state helpers from this
    # module's sibling, so a top-level import would close a cycle.
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
    )

    if is_owner_dashboard_request(request):
        # A permission DECISION is audited whichever way it goes. Recording only
        # refusals leaves the log unable to answer "who armed browsing on this
        # host", which is the question an investigation actually asks: the
        # damaging path here is an ALLOWED install or token write, not a blocked
        # one.
        _sel().log_api_access(
            caller=str(request.get("user") or "owner"),
            operation=operation,
            outcome="allowed",
            source="browser_api",
            resources=request.path,
        )
        return None
    # Derive a meaningful caller identity for the SEL record: app tokens
    # audit as "app:<name>"; dashboard users audit as their subject; callers
    # with no identity audit as "anonymous".
    app_name = request.get("app", "")
    if app_name:
        caller = f"app:{app_name}"
    else:
        caller = str(request.get("user") or "anonymous")
    # Permission denial on a security boundary — audited before the response
    # (backend-security-controls: every denial emits SEL).
    _sel().log_api_access(
        caller=caller,
        operation=operation,
        outcome="denied",
        source="browser_api",
        resources=request.path,
        error="browser mutations require the dashboard owner",
    )
    # Deny decision made above; only the response label changes for a signed
    # pre-owner bootstrap subject (see stale_owner_session_response).
    return _owner_denial_response(request, "dashboard user required", "dashboard_user_required")


async def api_browser_token_put(request: web.Request) -> web.Response:
    """PUT /api/browser/token -- set or clear the optional attach token.

    The response reports only WHETHER a token is set. A value that exists to reach a
    child process's environment has no reason to travel back out, and echoing it
    would put it in dashboard traffic and browser memory for no gain.

    Re-publishes the environment on the way out: a child reads the environment it
    was handed, so a token written without this would not reach any shell until the
    next gateway start.
    """
    denied = _deny_non_owner_browser_request(request, "browser_token_set")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Valid-but-non-object JSON ([], null, "str") would AttributeError on
    # body.get below -- an unintended 500 instead of a validation 400. Same
    # guard, same reason, as the other body-reading handlers in this file.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    value = body.get("token")
    if not isinstance(value, str):
        return web.json_response({"error": "token must be a string"}, status=400)
    await asyncio.to_thread(browser_cli_token.set_token, value)
    # Re-project onto the gateway's own environment, and DELETE the key when the
    # token was cleared: the agent inherits this env, so writing the file alone
    # would leave a revoked token live for every later invocation.
    #
    # Residual, stated rather than implied: a child process that ALREADY started
    # holds the value it inherited at spawn time, so an agent session running
    # right now keeps using the old token until it restarts. Revoking that would
    # mean killing live sessions on a settings write, which is a worse trade than
    # the window it closes.
    if browser_cli_token.has_token():
        os.environ.update(browser_cli_token.cli_env_overrides())
    else:
        os.environ.pop(browser_cli_token.TOKEN_ENV, None)
    return web.json_response({"ok": True, "token": browser_cli_token.has_token()})


async def api_browser_command(request: web.Request) -> web.Response:
    """POST /api/browser/command -- run one op against the native browser panel.

    Called by the ``browser`` MCP tool. Body:
    ``{"op": str, "session_key": str, "args"?: object, "timeout_ms"?: int}``.
    ``session_key`` is the BARE slot key the Electron panel registers under (the
    tool resolves and namespace-strips it; the ``dashboard:``-namespaced form
    travels in the ``X-Session-Key`` header for the peer check). Enqueues the op
    on the command bus and awaits the native panel's result.

    Responses:
    - 200 ``{"id", "ok": true, "result": <any>}`` -- op ran and succeeded;
    - 200 ``{"id", "ok": false, "error": str}`` -- op ran but failed;
    - 503 ``{"code": "no_native_panel"}`` -- no Electron poller for this session,
      returned FAST so the tool falls back to playwright-cli;
    - 429 ``{"code": "queue_full"}`` / 504 ``{"code": "timeout"}``.

    Proven ``X-Internal-Secret`` only (``request["internal_auth"] is True``),
    mirroring ``api_computer_use_invoke``: the middleware sets that flag only on
    the validated-secret path, so a dashboard COOKIE caller -- even a
    browser-credentialed page on a ``local_only=False`` deployment where strict
    paths reclassify as mixed -- is rejected here. We do NOT additionally require
    a loopback ``request.remote``: the tool reaches the gateway over the AF_UNIX
    internal-API socket (0700 data home + kernel peer check), where
    ``request.remote`` is empty, so a loopback re-assert would 403 every op.
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    op = body.get("op")
    args = body.get("args")
    timeout_ms = body.get("timeout_ms")
    session_key = body.get("session_key")
    if not isinstance(op, str) or not op:
        return web.json_response({"error": "op required", "code": "op_required"}, status=400)
    if args is not None and not isinstance(args, dict):
        return web.json_response(
            {"error": "args must be an object", "code": "args_must_be_object"}, status=400
        )
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
        timeout_ms = DEFAULT_COMMAND_TIMEOUT_MS
    if not isinstance(session_key, str) or not session_key:
        # No addressable panel -> answer like the no-panel case (503) so the tool
        # falls back to playwright-cli rather than surfacing a hard error.
        return web.json_response(
            {"error": "no-native-panel", "code": "no_native_panel"}, status=503
        )
    bus = get_command_bus()
    logger.debug("browser-cmdbus: submit op=%s session=%s", op, session_key)
    try:
        outcome = await bus.submit(session_key, op, args or {}, timeout_ms=timeout_ms)
    except NoPanelError:
        logger.debug(
            "browser-cmdbus: no native panel registered for session=%s -> 503 (client falls back to playwright-cli)",
            session_key,
        )
        return web.json_response(
            {"error": "no-native-panel", "code": "no_native_panel"}, status=503
        )
    except QueueFullError:
        return web.json_response({"error": "queue-full", "code": "queue_full"}, status=429)
    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout", "code": "timeout"}, status=504)
    response: dict[str, Any] = {"id": outcome.get("id"), "ok": bool(outcome.get("ok"))}
    if outcome.get("ok"):
        response["result"] = outcome.get("result")
    else:
        response["error"] = outcome.get("error") or "error"
    logger.debug(
        "browser-cmdbus: op=%s completed ok=%s session=%s", op, bool(outcome.get("ok")), session_key
    )
    return web.json_response(response)


async def api_browser_command_drain(request: web.Request) -> web.Response:
    """POST /api/browser/command-drain -- long-poll for a queued browser command.

    Called by the Electron main process. Body:
    ``{"session_keys": [str, ...], "wait_ms"?: int}``.

    SIDE EFFECT: registers ``session_keys`` as having a live native panel for a
    fixed liveness window (independent of ``wait_ms``) AND marks a native host
    present for the same window; the registration is what ``/api/browser/command``
    checks to decide whether to 503, and host-presence is what lets it briefly
    wait for a cold-starting panel instead. ``wait_ms == 0`` with empty
    ``session_keys`` is the Electron idle heartbeat: it refreshes host-presence
    and returns 204 at once.

    Responses: 200 ``{"id", "session_key", "op", "args"}`` or 204 (nothing yet).
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command_drain",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    session_keys = body.get("session_keys")
    if not isinstance(session_keys, list) or not all(isinstance(k, str) for k in session_keys):
        return web.json_response(
            {"error": "session_keys must be a list of strings", "code": "session_keys_invalid"},
            status=400,
        )
    wait_ms = body.get("wait_ms")
    # ``wait_ms == 0`` is a valid heartbeat: refresh host-presence and return 204
    # at once. Only a missing, negative, or non-int value takes the long wait.
    if not isinstance(wait_ms, int) or isinstance(wait_ms, bool) or wait_ms < 0:
        wait_ms = DEFAULT_DRAIN_WAIT_MS
    bus = get_command_bus()
    command = await bus.drain(session_keys, wait_ms=wait_ms)
    if command is None:
        return web.Response(status=204)
    return web.json_response(command)


async def api_browser_command_result(request: web.Request) -> web.Response:
    """POST /api/browser/command-result -- post a native browser command's result.

    Called by the Electron main process. Body:
    ``{"id": str, "ok": bool, "result"?: <any>, "error"?: str}``.

    Responses: 200 ``{"ok": true}``, or 404 ``{"code": "unknown_command"}`` when
    the id already timed out or never existed.
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command_result",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    command_id = body.get("id")
    if not isinstance(command_id, str) or not command_id:
        return web.json_response({"error": "id required", "code": "id_required"}, status=400)
    ok = bool(body.get("ok"))
    result = body.get("result")
    error = body.get("error")
    if error is not None and not isinstance(error, str):
        error = str(error)
    bus = get_command_bus()
    matched = await bus.complete(command_id, ok, result=result, error=error)
    if not matched:
        return web.json_response(
            {"error": "unknown-command", "code": "unknown_command"}, status=404
        )
    return web.json_response({"ok": True})


def _browser_install_job(state: DashboardState) -> browser_install_job.BrowserInstallJob | None:
    """The gateway's current or most recent install job, if one ran."""
    job = getattr(state, "_browser_install_job", None)
    return job if isinstance(job, browser_install_job.BrowserInstallJob) else None


def _browser_install_active(state: DashboardState) -> bool:
    """Whether an install occupies the gateway's one slot.

    The task is consulted as well as the job so the slot stays closed until
    the task that owns the worker has actually returned.
    """
    job = _browser_install_job(state)
    task = getattr(state, "_browser_install_task", None)
    return bool((job is not None and job.running) or (task is not None and not task.done()))


def _browser_install_status(state: DashboardState) -> dict[str, Any]:
    """The job-derived fields every install response carries.

    ``installing`` and ``last_error`` keep their meaning for older dashboards:
    whether a job runs, and the latest job's detail when it failed.
    """
    job = _browser_install_job(state)
    failed = job is not None and job.status == browser_install_job.STATUS_FAILED
    return {
        "installing": _browser_install_active(state),
        "install_job": job.snapshot() if job is not None else None,
        "last_error": job.error_detail if failed and job is not None else None,
    }


def _browser_install_conflict(state: DashboardState) -> web.Response:
    """409 naming the job that holds the slot, so the panel can say which one."""
    job = _browser_install_job(state)
    return web.json_response(
        {
            "error": "an install is already running",
            "code": "install_already_running",
            "install_job": job.snapshot() if job is not None and job.running else None,
        },
        status=409,
    )


def _start_browser_install_job(
    state: DashboardState,
    kind: str,
    engine: str | None,
    work: Callable[[browser_cli_install.StageCallback], dict[str, Any]],
    default_step: str,
) -> None:
    """Publish a new running job, then start its worker.

    The job is on ``state`` before the task exists, so a status read that
    lands between this call and the worker's first stage already reports it.
    Stage updates arrive from the worker thread and are marshalled onto the
    loop; each is bound to THIS job object, whose own id and status checks make
    a late update from a finished job a no-op.

    Cancelling the task (gateway shutdown cancels every pending task) kills
    the installer's process tree through the job's
    :class:`~kiro_crew.browser_cli.install.InstallScope` before the task
    ends, because cancelling the awaiting coroutine alone leaves the worker
    thread and its subprocess running.
    """
    loop = asyncio.get_running_loop()
    job = browser_install_job.BrowserInstallJob.start(kind, engine)
    scope = browser_cli_install.InstallScope()
    state._browser_install_job = job
    state._browser_install_scope = scope

    def _on_stage(stage: str) -> None:
        # Runs on the worker thread; the loop owns the job.
        loop.call_soon_threadsafe(job.apply_stage, job.id, stage)

    async def _run() -> None:
        try:
            result = await asyncio.to_thread(
                browser_cli_install.run_in_scope, scope, work, _on_stage
            )
        except asyncio.CancelledError:
            await _terminate_install_scope(scope)
            job.finish(
                job.id,
                browser_install_job.STATUS_INTERRUPTED,
                browser_install_job.ERROR_INTERRUPTED,
                "interrupted: the gateway stopped this install",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            # Redact the full text, then truncate: see bounded_detail. Pinned by
            # test_a_credential_straddling_a_pre_redaction_cut_is_still_masked.
            job.finish(
                job.id,
                browser_install_job.STATUS_FAILED,
                browser_install_job.ERROR_EXCEPTION,
                browser_install_job.bounded_detail(str(exc)),
            )
            return
        status, code, detail = browser_install_job.outcome_of(result, default_step)
        if scope.terminated and status != browser_install_job.STATUS_INTERRUPTED:
            status = browser_install_job.STATUS_INTERRUPTED
            code = browser_install_job.ERROR_INTERRUPTED
        job.finish(job.id, status, code, detail)

    state._browser_install_task = asyncio.create_task(_run())


async def _terminate_install_scope(scope: Any) -> None:
    """Kill an install scope's children off the event loop.

    ``InstallScope.terminate`` runs ``taskkill`` on Windows, a blocking call with
    its own timeout, so it goes to a worker thread. At interpreter teardown the
    default executor may already refuse work; the call then runs inline, since
    nothing else is left on the loop to stall.
    """
    try:
        await asyncio.to_thread(scope.terminate)
    except RuntimeError:
        scope.terminate()


async def stop_browser_install(state: DashboardState) -> None:
    """Terminate the running install's subprocesses and wait for its task.

    For the gateway's shutdown path. Idempotent and never raises. The job is
    left ``interrupted``; nothing is persisted or resumed, so the next gateway
    starts with no job and the operator retries explicitly.
    """
    scope = getattr(state, "_browser_install_scope", None)
    if isinstance(scope, browser_cli_install.InstallScope):
        await _terminate_install_scope(scope)
    task = getattr(state, "_browser_install_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        # ``wait`` reports the child's outcome instead of raising it here, so the
        # CancelledError the child raises because it was just cancelled never
        # reaches this frame, while a cancellation of THIS task (the gateway's
        # shutdown deadline expiring around ``_shutdown()``) still propagates out
        # of the await. A plain ``await task`` inside ``except CancelledError``
        # could not tell the two apart and swallowed both.
        await asyncio.wait({task})
        if not task.cancelled():
            # Consume a failure so the loop does not log "exception was never
            # retrieved" at teardown; the job record already carries it.
            task.exception()


async def api_browser_install_get(request: web.Request) -> web.Response:
    """GET /api/browser/install -- whether browsing is available, and why not.

    Reports the install job separately from the detection fields so the card can
    show progress for an install already in flight, including one started by a
    different dashboard tab: the job lives on the gateway, not in a page. A read
    never spawns an installer, launches a browser, or attaches to one.
    """
    state: DashboardState = request.app["state"]
    payload = dict(await asyncio.to_thread(browser_cli_install.detect))
    payload.update(_browser_install_status(state))
    payload["token"] = browser_cli_token.has_token()
    return web.json_response(payload)


async def api_browser_install_start(request: web.Request) -> web.Response:
    """POST /api/browser/install -- install the Playwright CLI in the background.

    Returns immediately with the same shape as the GET. The install downloads a
    browser, which takes long enough that holding the request open would read as a
    hung dashboard, so progress is observed by re-reading rather than awaited here.

    A click while CLI setup already runs is folded into that job: it is the same
    work, and npm is not safe to run twice over the same target at once. A click
    while an ENGINE download runs is refused with 409 naming that job, because
    folding it would answer "CLI setup accepted" for work that is not happening.
    """
    denied = _deny_non_owner_browser_request(request, "browser_cli_install")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    if _browser_install_active(state):
        job = _browser_install_job(state)
        if job is not None and job.running and job.kind != browser_install_job.KIND_CLI_SETUP:
            return _browser_install_conflict(state)
    else:
        _start_browser_install_job(
            state,
            browser_install_job.KIND_CLI_SETUP,
            None,
            lambda on_stage: browser_cli_install.install(on_stage=on_stage),
            "install",
        )
    return await api_browser_install_get(request)


async def api_browser_engine_install(request: web.Request) -> web.Response:
    """POST /api/browser/engine -- download one engine's browser build.

    Body: ``{"engine": "chromium" | "firefox" | "webkit"}``.

    Shares the ONE install slot with the CLI install rather than taking its own:
    both drive the same browser installer, which is not safe to run twice over the
    same cache at once.
    """
    denied = _deny_non_owner_browser_request(request, "browser_engine_install")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a crash
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # `(body or {})` absorbs [] and null but NOT a non-empty non-dict ([1],
    # "abc", 5), which would AttributeError into a 500. Check the type instead
    # of leaning on falsiness.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    engine = str(body.get("engine", "")).strip()
    # Validated here as well as in install_browser: rejecting at the boundary
    # keeps an unknown value out of the background task entirely, so the operator
    # gets a 400 instead of an error they have to go re-read the status to find.
    if engine not in browser_cli_install.BROWSER_ENGINES:
        return web.json_response({"error": "unknown engine", "code": "unknown_engine"}, status=400)
    if _browser_install_active(state):
        # 409, NOT a folded success. Engines are three DISTINCT targets sharing
        # one slot, so answering 200 while a different engine installs makes the
        # panel show WebKit downloading when Firefox actually is.
        return _browser_install_conflict(state)
    _start_browser_install_job(
        state,
        browser_install_job.KIND_ENGINE_DOWNLOAD,
        engine,
        lambda on_stage: browser_cli_install.install_browser(engine, on_stage=on_stage),
        "install-browser",
    )
    return await api_browser_install_get(request)


def _browser_view_payload() -> dict[str, Any]:
    """``browser_cli_view.status()`` plus where the panel should FRAME it.

    ``path`` is the dashboard-origin relay (``/browser-view/<token>/``): same
    origin as the dashboard, so it is reachable wherever the dashboard is — an
    SSH forward, a tunnel — with no second port. The embedded per-instance
    capability token IS the relay's authentication (the panel frames it in an
    opaque-origin sandbox that sends no cookies), and THIS payload — served
    only through the cookie-authed, owner-gated view endpoints — is its sole
    disclosure point, so possession proves the holder passed the owner gate.
    Null unless the view is running, so the field can never frame a dead
    relay. The absolute ``url`` stays in the payload for direct loopback use
    and older frontends — but it is only ever published alongside a target
    the ownership proof vouched for: when ``relay_target()`` refuses (the
    child exited between the two lock holds, or the proof was inconclusive),
    the whole payload degrades to ``stopped`` rather than offering the stale
    direct ``url`` as a frameable fallback for whatever wins the freed port.
    The panel polls, so a live view is re-reported on the next cycle.
    """
    payload = browser_cli_view.status()
    if payload.get("status") != "running":
        payload["path"] = None
        return payload
    target = browser_cli_view.relay_target()
    if target is None:
        payload["status"] = "stopped"
        payload["url"] = None
        payload["port"] = None
        payload["path"] = None
        return payload
    payload["path"] = f"{ROUTE_PREFIX}/{target[1]}/"
    return payload


async def api_browser_view_get(request: web.Request) -> web.Response:
    """GET /api/browser/view -- where the Playwright CLI dashboard is served.

    Reports without starting anything, so polling the panel never launches a
    browser dashboard the operator did not ask for.

    App-token denied like the install and token routes: the reply carries the
    dashboard URL, and that URL is served WITHOUT authentication, so handing it
    to an app is handing over control of a logged-in browser. Read-only on this
    gateway is not read-only on the browser. (The ``path`` field embeds the
    relay's capability token — this owner-gated endpoint is that token's only
    disclosure point, so it stays denied to apps for the same reason as the
    raw URL.)
    """
    denied = _deny_non_owner_browser_request(request, "browser_view_status")
    if denied is not None:
        return denied
    return web.json_response(await asyncio.to_thread(_browser_view_payload))


async def api_browser_view_start(request: web.Request) -> web.Response:
    """POST /api/browser/view/start -- ensure the CLI dashboard is serving.

    Idempotent, and off-loaded to a thread because starting the dashboard waits on
    a child process becoming healthy, which would otherwise stall the event loop
    and with it every other dashboard request. Honors
    ``dashboard.browser_view_port`` when set, so a remote-gateway operator can
    keep the port inside their tunnel's forwarded set.

    App-token denied: this both LAUNCHES a browser process and returns the
    unauthenticated dashboard URL, so it is the stronger half of the same hole
    the GET carries.
    """
    denied = _deny_non_owner_browser_request(request, "browser_view_start")
    if denied is not None:
        return denied

    def _start_view() -> None:
        # Config read stays in the worker thread with the child-process wait:
        # both are blocking I/O that must not run on the event loop.
        pinned = KiroCrewConfig.load().dashboard.browser_view_port
        browser_cli_view.ensure_running(pinned or None)

    await asyncio.to_thread(_start_view)
    return web.json_response(await asyncio.to_thread(_browser_view_payload))


async def api_browser_open(request: web.Request) -> web.Response:
    """POST /api/browser/open -- show a URL the owner typed in the gateway's browser.

    Body: ``{"url": "https://...", "session_key": "<chat slot>"}``. The Browser
    panel calls this on the non-native transport (a plain browser tab, including
    one reaching a remote gateway over a tunnel) when the typed host is not
    loopback: nothing else can render an external site there, because the
    dashboard CSP refuses to frame it. The handler makes sure the ``show`` view is
    serving, runs ``playwright-cli -s=<session> goto|open <url>`` as a supervised
    child (see :mod:`kiro_crew.browser_cli.launcher`), and answers ``{ok,
    session, error, view}`` -- ``error`` is the CLI's own text, verbatim, so the
    panel can show WHY (``No usable sandbox!``, a missing Chromium build) instead
    of a blank frame; ``view`` is the post-attempt ``show`` status so the panel
    can frame it without a second round trip.

    Owner-only, like the view routes, and it additionally REFUSES a caller
    authenticated by the internal secret: agent browsing goes through the shell
    approval ladder (capability model), and an agent reaching this endpoint would
    bypass it. A human pressing Enter in an authenticated dashboard is the consent
    this route rests on. Off-loaded to a thread because ``open`` waits on a
    Chromium launch, which must not stall the event loop.
    """
    denied = _deny_non_owner_browser_request(request, "browser_open")
    if denied is not None:
        return denied
    if request.get("internal_auth"):
        # Belt and braces: the route is not on any internal-path list, so the
        # secret is ignored before this handler runs; this pins the refusal even
        # if the route is ever added to one.
        _sel().log_api_access(
            caller="internal",
            operation="browser_open",
            outcome="denied",
            source="browser_api",
            resources=request.path,
            error="internal-secret callers may not drive the browser panel",
        )
        return web.json_response(
            {"error": "forbidden for internal callers", "code": "internal_caller"}, status=403
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a crash
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    raw_url = body.get("url")
    session_key = body.get("session_key")
    if not isinstance(raw_url, str) or not isinstance(session_key, str) or not session_key.strip():
        return web.json_response(
            {"error": "url and session_key are required", "code": "invalid_request"}, status=400
        )
    url = browser_cli_launcher.validate_url(raw_url)
    if url is None:
        return web.json_response(
            {
                "error": (
                    "url must be http(s) with a host and no credentials, "
                    "query, or fragment (a secret in the URL would leak via argv)"
                ),
                "code": "invalid_url",
            },
            status=400,
        )

    def _launch() -> dict[str, Any]:
        # Same start path as api_browser_view_start, in the worker thread with the
        # child-process wait: the view must be up for the human to see the page,
        # and its singleton socket is what the launcher's reveal talks to.
        pinned = KiroCrewConfig.load().dashboard.browser_view_port
        browser_cli_view.ensure_running(pinned or None)
        result = browser_cli_launcher.open_url(url, session_key.strip())
        payload = result.as_dict()
        payload["view"] = _browser_view_payload()
        return payload

    return web.json_response(await asyncio.to_thread(_launch))

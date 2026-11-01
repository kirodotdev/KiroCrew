"""The in-app update step-up: an agent or the panel arms an update, a human approves it.

``POST /api/update/arm``, its status and disarm routes, ``POST /api/update/approve``,
the packaged desktop app's arm path, and the audit every step-up route writes.

Served through :mod:`kiro_crew.dashboard.handlers.updates`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from kiro_crew.dashboard.handlers.updates_apply import _restart_gateway
from kiro_crew.dashboard.handlers.updates_check import _do_update_check
from kiro_crew.dashboard.handlers.updates_verdict import (
    _display_version,
    _downgrade_target_below_min_version,
    _update_info,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.platform.update_capability import CHECK_FAILED
from kiro_crew.platform.update_layout import release_channel as _release_channel
from kiro_crew.platform.update_provider import resolve_provider

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")


# ── In-app wheel update: arm + host-local approve (RFC OQ7) ──


def _loopback_peer(request: web.Request) -> bool:
    """Did this request arrive from the host? Defence in depth, not authority.

    The NONCE is the authority — it proves the caller read the gateway host's
    filesystem. This check just refuses the obviously-remote shape early, and
    is knowingly imperfect behind same-host proxies, which is exactly why it is
    not the boundary.

    Composed from the SHARED predicates rather than a bespoke IP list: an
    AF_UNIX caller has an EMPTY ``request.remote`` (token_auth documents
    this), so an IP-only test would 403 the CLI's PREFERRED transport — the
    unix socket, whose SO_PEERCRED check is stronger host-locality evidence
    than any IP. The auth middleware's ``internal_auth`` / ``peer_verified``
    marks are honoured too, so a local-secret-authenticated caller passes
    however it connected.
    """
    if request.get("internal_auth") or request.get("peer_verified"):
        return True
    from kiro_crew.dashboard.origin import request_is_unix_socket
    from kiro_crew.dashboard.urls import is_loopback

    if request_is_unix_socket(request):
        return True
    return is_loopback(request.remote or "")


async def _audit_update_event(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    error: str = "",
    resources: str = "",
    required: bool = False,
) -> None:
    """Write one update event to SEL without blocking the gateway loop.

    A granted approval is audit-or-deny when ``required`` is true. Denials stay
    best-effort: failure to record a refusal must never turn it into permission.
    """

    def _write() -> None:
        # Function-local: keep SEL initialization/import work off the boot path.
        from kiro_crew.sel import sel as _sel

        try:
            _sel().log_api_access(
                caller="host-cli" if request.get("internal_auth") else (request.remote or "unix"),
                operation=operation,
                outcome=outcome,
                source="dashboard",
                resources=resources,
                error=error,
                critical=True,
            )
        except Exception:
            if required:
                raise
            logger.debug("SEL audit for %s failed", operation, exc_info=True)

    # A CRITICAL SEL write flushes inline on its calling thread by design.
    await asyncio.to_thread(_write)


async def _arm_packaged_app(request: web.Request) -> web.Response:
    """Arm an update REQUEST for a packaged desktop install (dmg/appimage/deb/rpm/nsis).

    Not a step-up. The managed-venv lane needs one because the gateway itself
    performs the apply, so something has to prove a human authorised it. On a
    packaged install the gateway performs nothing: the bytes belong to the
    desktop app's updater, and the only control that starts it is the click in
    Settings › About inside the app's own renderer. That click IS the approval.
    So the record this writes is a nudge with context — which version, who
    asked, when — and carries no nonce, no token, nothing an approval could
    present. There is no gateway endpoint that turns it into an install, which
    is what makes agent self-approval impossible by construction rather than by
    fence: an agent can read its own request and gain nothing from doing so.

    The requester may NAME a version (``{"version": "0.6.0"}``); absent, the
    request is for whatever the feed offers. Display-only either way — the
    app's updater decides what downloads, and the panel says when they differ.
    """
    from kiro_crew.platform.app_update_request import get_app_update_requests

    if resolve_provider() is not None:
        error = "updates on this host are managed by policy"
        await _audit_update_event(request, operation="update.arm", outcome="denied", error=error)
        return web.json_response(
            {"error": error, "code": "arm_policy_managed", "governance": True}, status=409
        )
    try:
        body = await request.json()
    except Exception:
        body = None
    version = ""
    if isinstance(body, dict):
        raw = body.get("version")
        if raw is not None and not isinstance(raw, str):
            return web.json_response(
                {"error": "version must be a string", "code": "invalid_version"}, status=400
            )
        version = (raw or "").strip()
    if version and _downgrade_target_below_min_version(
        version, await asyncio.to_thread(_release_channel)
    ):
        error = "selected release is below the required minimum version"
        await _audit_update_event(
            request, operation="update.arm", outcome="denied", error=error, resources=f"v{version}"
        )
        return web.json_response(
            {"error": error, "code": "arm_below_min_version", "governance": True}, status=409
        )
    # Who asked, for the panel. A session key names the agent conversation; a
    # user mark names a dashboard login; neither is trusted for anything but
    # display, so a spoofed value costs nothing.
    requested_by = str(request.get("user") or request.headers.get("X-Session-Key") or "dashboard")[
        :64
    ]
    req, is_new_ask = await asyncio.to_thread(
        get_app_update_requests().arm, target_version=version, requested_by=requested_by
    )
    state: DashboardState = request.app["state"]
    label = f"v{version}" if version else "the latest version"
    if is_new_ask:
        # The existing notification path: this is what reaches a chat-only
        # user's bell, so their next visit to the app lands on the request.
        # Once per distinct ask — a looping agent turn re-arming the same
        # request must not ring the bell once per iteration.
        state.notify(
            "update",
            "An agent requested an app update",
            f"{requested_by} asked to update Kiro Crew to {label}. Approve it in Settings › About.",
            url="/settings/about",
        )
    await _audit_update_event(
        request,
        operation="update.arm",
        outcome="granted",
        resources=f"{label} (app lane, by {requested_by})",
    )
    return web.json_response({"ok": True, **req.to_public(time.time())})


async def api_update_arm(request: web.Request) -> web.Response:
    """POST /api/update/arm — arm a pending in-app update (SPA-callable).

    Which lane the arm records is derived from the INSTALL SHAPE, through the
    one derivation every update surface shares. A packaged desktop install gets
    a nonce-free request that the app's own About panel turns into an install
    with a human click (see :func:`_arm_packaged_app`). A managed venv gets the
    step-up below.

    Arming grants nothing on either lane. For the managed venv it records the
    request and writes the approval nonce to a file only the host can read; the
    response NEVER carries the nonce. Refused for every other shape, when a
    downgrade would cross below the active minimum-version floor, and when
    neither a newer update nor a pending channel move is cached — an arm must
    name the version the check reported, not whatever the feed happens to
    serve later (the apply re-verifies against the signed manifest anyway).

    The managed-venv lane RE-CHECKS FIRST, because the version armed here is the
    version installed:
    ``_update_info`` is refreshed by a background poll only every 12 hours, and
    the apply refuses outright when the feed has moved past the armed version
    (``wheel_engine._apply_locked``). Arming a half-day-old verdict therefore
    either pins the user to a build the feed has already superseded or dead-ends
    the whole flow — arm, host approval, refusal, start over. One request against
    the live feed here is what makes an approval mean "install the newest". It
    FAILS CLOSED: a check that cannot reach the feed refuses the arm as having no
    verdict, because the apply fetches the signed manifest before it installs
    anything — an arm the feed cannot confirm now is an approval the apply cannot
    honour a moment later.
    """
    # Function-local: boot-path rule, same as _restart_gateway's import.
    from kiro_crew.platform import update_stepup
    from kiro_crew.platform.update_capability import MANAGED_BY_ELECTRON, derive_capability
    from kiro_crew.platform.wheel_engine import running_from_managed_venv

    # Offloaded: the derivation shells out to git and touches disk.
    capability = await asyncio.to_thread(derive_capability)
    if capability.managed_by == MANAGED_BY_ELECTRON:
        return await _arm_packaged_app(request)

    if not await asyncio.to_thread(running_from_managed_venv):
        return web.json_response(
            {
                "error": (
                    "in-app update applies only to the cli.sh managed-venv install "
                    "and the packaged desktop app"
                ),
                "code": "arm_wrong_shape",
            },
            status=409,
        )
    if resolve_provider() is not None:
        return web.json_response(
            {
                "error": "updates on this host are managed by policy",
                "code": "arm_policy_managed",
            },
            status=409,
        )
    # AFTER the shape/policy refusals above: neither can be changed by a check,
    # and a host that cannot apply an update at all should not pay a feed round
    # trip to be told so.
    #
    # The check is a SHARED AWAITED TASK (`_do_update_check`): a status poll that
    # already holds the worker does not turn this call into a no-op that returns the
    # cache — the very stale verdict this re-check exists to replace, armed silently
    # and then rejected later by approval. The await below joins the running check
    # and reads ITS answer, and re-runs when a channel switch superseded it — the
    # same contract the Electron twin gives the click via `checkInFlight` in
    # website/electron/runtime/update/feed-lane.js.
    #
    # A check that cannot reach the feed leaves the FAILED result in the cache
    # (`_set_update_info` replaces it wholesale, so `update_available` is blank) and
    # the arm falls through to the no-verdict refusal below. There is no fallback to
    # the snapshot taken before the await: unlike the desktop updater, whose staged
    # bytes are already on disk, the managed-venv apply fetches the signed manifest
    # first (`wheel_engine._apply_locked`), so an arm the feed could not confirm is
    # an approval the apply cannot carry out. The panel reads the same cache off the
    # status frame and shows the failed check, which is what the refusal describes.
    #
    # The snapshot survives for one judgement only: whether the offer the user
    # clicked was an UPGRADE, which the fresh verdict alone cannot say.
    clicked_info = dict(_update_info)
    await _do_update_check()
    check_failed = _update_info.get("check_status") == CHECK_FAILED
    available = _update_info.get("update_available")
    move_pending = _update_info.get("channel_move_pending")
    version = str(_update_info.get("latest_version") or "")
    channel = str(_update_info.get("channel") or "")
    # The offer the user clicked was an UPGRADE (`update_available` in the
    # snapshot). If the fresh verdict is only a pending channel move, the lane
    # was rolled back below the running build in between, and arming it would
    # turn an upgrade click into a downgrade nobody chose. The clicked offer is
    # withdrawn, but "you're already up to date" would be false one status
    # frame later, when the panel offers that move, so it has its own code.
    if (
        available is False
        and move_pending is True
        and not check_failed
        and clicked_info.get("update_available") is True
    ):
        return web.json_response(
            {"error": "This update is no longer offered", "code": "arm_offer_withdrawn"},
            status=409,
        )
    if available is False and move_pending is not True and not check_failed:
        # A POSITIVE "nothing for you" — the re-check above reached the feed and the
        # offer the user clicked is gone (withdrawn, or they are already current).
        # Kept apart from the no-verdict refusal below because "run a check first"
        # would be both a wrong statement of fact and useless advice on this path:
        # a check just ran, and another gives the same answer. The offer itself also
        # disappears from every session within one 5s status frame (the re-check
        # left `update_available: False` in the cache), so a message telling the
        # user to re-check for it would send them after something already gone.
        #
        # `check_failed` is what earns the withdrawal claim: a failed check blanks
        # the verdict rather than answering False, and any False that did survive
        # beside it is not an answer from the feed. That path falls through to the
        # no-verdict refusal below, which is the honest reply when nothing fresh is
        # known.
        #
        # `move_pending` excuses the same `available is False`: a pending channel
        # move is armable on its own, and it reads as False here precisely because
        # the target lane offers no version-bearing update. Firing this refusal on
        # it would report a withdrawn offer for a move the feed never offered one
        # for, so the pending move is what the guard below judges instead.
        return web.json_response(
            {
                "error": "This update is no longer offered — you're already up to date",
                "code": "arm_no_longer_offered",
            },
            status=409,
        )
    if (available is not True and move_pending is not True) or not version:
        return web.json_response(
            {
                "error": "no update-available verdict — run a check first",
                "code": "arm_no_verdict",
            },
            status=409,
        )
    if _downgrade_target_below_min_version(version, channel):
        error = "selected release is below the required minimum version"
        await _audit_update_event(
            request,
            operation="update.arm",
            outcome="denied",
            error=error,
            resources=f"v{version} ({channel})",
        )
        return web.json_response(
            {
                "error": error,
                "code": "arm_below_min_version",
                "governance": True,
            },
            status=409,
        )
    try:
        pending = await asyncio.to_thread(update_stepup.arm, version, channel, source="dashboard")
    except update_stepup.StepUpError as exc:
        return web.json_response({"error": str(exc), "code": "arm_failed"}, status=500)
    return web.json_response(
        {
            "ok": True,
            **update_stepup.public_view(pending),
            # The re-check above can arm a NEWER version than the button the user
            # clicked named (that label comes from a verdict up to 12 hours old),
            # so the armed panel has to be able to say what will actually
            # install. Display-only sibling of the raw `version` in the view,
            # same fold and same reason as `latest_version_display`: the raw
            # stamp of a promoted stable build reads `0.4.1rc1`, and naming a
            # prerelease the user never chose is its own dishonesty.
            "version_display": _display_version(pending.version, pending.channel),
        }
    )


async def api_update_arm_status(request: web.Request) -> web.Response:
    """GET /api/update/arm — the armed request, SPA-safe projection.

    Either lane's record, whichever exists; arm dispatches on install shape so
    at most one does. The packaged-app projection carries ``managed_by:
    "electron"`` so the About panel can offer the in-app Install click rather
    than the host command the managed-venv lane needs.
    """
    from kiro_crew.platform import update_stepup
    from kiro_crew.platform.app_update_request import get_app_update_requests

    # clear_expired=True: this runs inside the gateway, where the expiry
    # cleanup is serialized against arm under the module mutex.
    pending = await asyncio.to_thread(lambda: update_stepup.read_pending(clear_expired=True))
    if pending is not None:
        return web.json_response({**update_stepup.public_view(pending), "managed_by": "kirocrew"})
    requests = get_app_update_requests()
    req = await asyncio.to_thread(requests.current)
    if req is None:
        return web.json_response({"armed": False})
    # Policy is re-checked at PUBLISH, not only at arm. The record lives in a
    # file an agent can write, so a request that would have been refused at arm
    # — a policy-pinned host, a version below the floor — must not surface a
    # card just because it arrived by another route. A refused record is also
    # dropped, so the card does not flicker back on the next poll.
    if resolve_provider() is not None or (
        req.target_version
        and _downgrade_target_below_min_version(
            req.target_version, await asyncio.to_thread(_release_channel)
        )
    ):
        await asyncio.to_thread(requests.clear)
        await _audit_update_event(
            request,
            operation="update.arm",
            outcome="denied",
            error="request refused by update policy at publish",
            resources=f"v{req.target_version or 'latest'} ({req.request_id})",
        )
        return web.json_response({"armed": False})
    return web.json_response(req.to_public(time.time()))


async def api_update_disarm(request: web.Request) -> web.Response:
    """DELETE /api/update/arm?request_id=… — decline a packaged-app update request.

    SPA-callable for the same reason arming is: it removes a nudge rather than
    granting anything. Without it the About panel's request card would offer
    only the control that installs, for the whole TTL, which is pressure and
    not consent.

    ``request_id`` is REQUIRED and names the request the panel SHOWED. Only that
    request is removed: a click on a card rendering request A must not erase a
    request B an agent made after the render, because B is a decision the user
    has not seen. A stale id is not an error — the answer says what is live
    now, and the panel's next poll renders it.

    Packaged lane only. The managed-venv step-up's nonce file is a different
    record with its own lifecycle and is not touched here.
    """
    from kiro_crew.platform.app_update_request import get_app_update_requests

    request_id = request.query.get("request_id", "")
    if not request_id:
        return web.json_response(
            {"error": "request_id is required", "code": "invalid_request_id"}, status=400
        )
    requests = get_app_update_requests()
    removed = await asyncio.to_thread(requests.decline, request_id)
    if removed:
        await _audit_update_event(
            request, operation="update.disarm", outcome="success", resources=request_id
        )
    still = await asyncio.to_thread(requests.current)
    return web.json_response({"ok": True, "armed": still is not None, "dismissed": removed})


async def api_update_approve(request: web.Request) -> web.Response:
    """POST /api/update/approve — consume the nonce and run the shadow apply.

    Called by ``kirocrew update approve`` on the gateway host, which read the
    nonce from the data home. On success the apply runs as a background task
    through :func:`kiro_crew.platform.wheel_apply.run_wheel_apply`, the path the
    gateway's unattended apply runs too: shadow build + verify + promote (all
    off-loop), then the shared gateway restart, with progress on the same SSE
    feed the git apply uses. A shutdown or an exec restart stops it first.
    """
    if not _loopback_peer(request):
        return web.json_response(
            {
                "error": "approval is accepted from the gateway host only",
                "code": "approve_not_local",
            },
            status=403,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("nonce"), str):
        return web.json_response(
            {"error": "nonce must be a string", "code": "invalid_nonce"}, status=400
        )
    # Function-local: boot-path rule, same as the other update handlers.
    from kiro_crew.platform import update_stepup, wheel_apply
    from kiro_crew.platform.wheel_engine import (
        WheelUpdateError,
        check_release_version,
        hold_update_lock,
        release_update_lock,
        respawn_executable,
    )

    # A policy-defined command provider OWNS updates on this host, and its
    # commands never read the built-in mechanism this endpoint drives. Checked
    # at APPROVE time, not just at arm: a provider installed in the window
    # between the two must win — the armed request predates the policy, and a
    # host approval is not authority to bypass it.
    if resolve_provider() is not None:
        return web.json_response(
            {
                "error": "updates on this host are managed by policy",
                "code": "approve_policy_managed",
                "governance": True,
            },
            status=409,
        )
    # Every refusal that does not depend on the approval itself runs BEFORE the
    # nonce is consumed, so a refused attempt leaves the armed request intact:
    # the preflight (a pinned fleet's policy decides where this host may take
    # code from, and a host approval is not that authority), the memory copy's
    # readiness, and the update lock, which is then HELD through the apply so
    # "is another update running" is decided exactly once.
    try:
        feed_base, artifact_base = wheel_apply.preflight_bases()
    except wheel_apply.WheelApplyRefused as exc:
        if exc.code == "blocked_by_policy":
            logger.warning("In-app update approval refused by source pin: %s", exc.message)
            return web.json_response(
                {"error": exc.message, "code": "approve_blocked_by_policy", "governance": True},
                status=403,
            )
        return web.json_response({"error": exc.message, "code": "approve_bad_cdn"}, status=409)
    try:
        await asyncio.to_thread(wheel_apply.check_memory_ready)
    except WheelUpdateError as exc:
        outcome = wheel_apply.classify(exc)
        code = "approve_memory_preparing" if outcome.status == "deferred" else "approve_memory"
        return web.json_response({"error": outcome.message, "code": code}, status=409)
    state: DashboardState = request.app["state"]
    if state._gateway_restart_in_progress is True:
        # The restart's exec would stop the apply before it could be audited.
        return web.json_response(
            {
                "error": "the gateway is restarting; approve the update again once it is back",
                "code": "approve_restarting",
            },
            status=409,
        )
    try:
        lock_fd = await asyncio.to_thread(hold_update_lock)
    except WheelUpdateError as exc:
        outcome = wheel_apply.classify(exc)
        code = "approve_busy" if outcome.status == "busy" else "approve_lock_failed"
        return web.json_response({"error": outcome.message, "code": code}, status=409)

    # SEL-audited at every verdict: an approval is a code-install
    # authorization, which is exactly the class of event the audit chain
    # exists to reconstruct. `caller` is the transport identity — the nonce
    # proves host-locality, not a person.
    async def _audit(
        outcome: str, error: str = "", resources: str = "", required: bool = False
    ) -> None:
        await _audit_update_event(
            request,
            operation="update.approve",
            outcome=outcome,
            error=error,
            resources=resources,
            required=required,
        )

    async def _refuse(response: web.Response) -> web.Response:
        await asyncio.shield(asyncio.to_thread(release_update_lock, lock_fd))
        return response

    try:
        pending = await asyncio.to_thread(update_stepup.consume, body["nonce"])
    except update_stepup.StepUpError as exc:
        await _audit("denied", error=str(exc))
        return await _refuse(
            web.json_response({"error": str(exc), "code": "approve_refused"}, status=403)
        )
    try:
        check_release_version(pending.version)
    except WheelUpdateError as exc:
        await _audit("denied", error=str(exc))
        return await _refuse(
            web.json_response({"error": str(exc), "code": "approve_refused"}, status=409)
        )
    if _downgrade_target_below_min_version(pending.version, pending.channel):
        error = "selected release is below the required minimum version"
        await _audit(
            "denied",
            error=error,
            resources=f"v{pending.version} ({pending.channel})",
        )
        return await _refuse(
            web.json_response(
                {
                    "error": error,
                    "code": "approve_below_min_version",
                    "governance": True,
                },
                status=409,
            )
        )
    # The request was armed on a channel; a policy pinned since (``updates.channel``)
    # decides which channel this host may take code from, as the source pin does.
    # The same check the engine runs under the lock, asked here so the refusal is
    # audited and named before the apply starts.
    from kiro_crew.platform.wheel_engine import check_policy_channel

    try:
        await asyncio.to_thread(check_policy_channel, pending.channel)
    except WheelUpdateError as exc:
        error = str(exc)
        await _audit(
            "denied",
            error=error,
            resources=f"v{pending.version} ({pending.channel})",
        )
        return await _refuse(
            web.json_response(
                {"error": error, "code": "approve_blocked_by_policy", "governance": True},
                status=403,
            )
        )
    try:
        await _audit("granted", resources=f"v{pending.version} ({pending.channel})", required=True)
    except Exception:
        # The armed request is already consumed (single-use), so refusing here
        # costs the operator a re-arm — the fail-closed direction: no code
        # install proceeds without its audit record.
        logger.error(
            "update.approve audit could not be written; refusing unaudited install",
            exc_info=True,
        )
        return await _refuse(
            web.json_response(
                {
                    "error": "approval audit could not be recorded; the update was not started",
                    "code": "approve_audit_failed",
                },
                status=503,
            )
        )

    # Asked before the promotion moves the launcher it compares against.
    reattach = await asyncio.to_thread(wheel_apply.userns_reattach_needed, pending.version)

    async def _apply() -> None:
        state.push_refresh("updating")
        outcome = await wheel_apply.run_wheel_apply(
            channel=pending.channel,
            version=pending.version,
            feed_base=feed_base,
            artifact_base=artifact_base,
            state=state,
            held_lock_fd=lock_fd,
        )
        resources = f"v{pending.version}"
        if outcome.status == "promoted":
            await _audit("success", resources=f"{resources} promoted")
            if reattach:
                state.notify(
                    "update",
                    f"Kiro Crew {pending.version}: re-attach the sandbox profile",
                    wheel_apply.userns_reattach_after_apply(pending.version),
                )
            if not await asyncio.to_thread(wheel_apply.restart_reaches, pending.version):
                # A restart would exec the running version again; say so instead.
                remedy = wheel_apply.restart_unreachable_remedy(pending.version, pending.channel)
                logger.error("In-app wheel update to v%s: %s", pending.version, remedy)
                state.push_update_progress("failed", remedy)
                state.push_refresh("update_failed")
                return
            logger.info("In-app wheel update to v%s promoted; restarting", pending.version)
            await _restart_gateway(state, resolver=respawn_executable)
            return
        if outcome.status == "cancelled":
            # A deliberate stop (a shutdown, an exec), not a failed install: no
            # failure is pushed over the restart or shutdown the stop belongs to.
            await _audit("cancelled", error=outcome.message, resources=resources)
            return
        await _audit("failed", error=outcome.message, resources=resources)
        detail = outcome.message
        if outcome.status == "incompatible":
            detail = f"{detail}. {wheel_apply.incompatible_remedy(pending.channel)}"
        state.push_update_progress("failed", detail)
        state.push_refresh("update_failed")

    task = asyncio.create_task(_apply())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "status": "applying", "version": pending.version})

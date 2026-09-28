"""Owner-gated dashboard endpoints for flagged-file delivery consent.

The ONLY writer of ``file_delivery_consent.json``. The store sits on the keystone
floor so an agent cannot write it with file tools or a shell form. Recording a
grant takes TWO acts here: the owner-gated POST ARMS a request (writing a
single-use nonce to a keystone file the SPA can never read), and the loopback-only
approve endpoint -- driven by ``kirocrew file-delivery approve`` on the gateway
host -- consumes that nonce and records the grant. The CLI verb authorizes
nothing on its own; it proves host presence by reading a nonce an automated caller
cannot, which is what closes the "an owner-authenticated but agent-DRIVEN browser
self-grants" hole an owner-session identity check alone leaves open.

Every read/arm verb is refused to anyone but the dashboard OWNER. Three
callers had to be shut out and only the first is obvious:

* an AGENT: already blocked from the file itself by the keystone fence, but an
  app token declaring this route's permission would be the same door's second
  key, so the check is here too rather than relying on the fence alone.
* an APP token: an app could otherwise mint a grant with no human in the loop.
* an allowed MESSAGING user: a Slack allow-listed non-owner running
  ``!dashboard`` authenticates with ``app == ""``, so an app-only check would let
  them authorize delivery of the OWNER's secrets.

Reads are refused for their own reason: the response says which delivery
destinations the owner has blessed, which tells a caller where a flagged file
would land unrefused. ``is_owner_dashboard_request`` already encodes exactly the
rule needed (app present and empty, caller equal to ``owner_id`` or a local-owner
subject), so it is reused rather than re-derived.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from aiohttp import web

from kiro_crew import file_delivery_consent
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_UNKNOWN_CLASS = "unknown_destination_class"
_CODE_ARM_FAILED = "file_delivery_arm_failed"
_CODE_APPROVE_NOT_LOCAL = "file_delivery_approve_not_local"
_CODE_APPROVE_REFUSED = "file_delivery_approve_refused"
_CODE_APPROVE_COMPUTER_USE = "file_delivery_approve_computer_use_active"
_CODE_APPROVE_UNSANDBOXED = "file_delivery_approve_unsandboxed"
_CODE_APPROVE_WRITE_FAILED = "file_delivery_approve_write_failed"
_CODE_INVALID_NONCE = "invalid_nonce"


def _approve_is_local(request: web.Request) -> bool:
    """Whether an approve request is coming from the gateway HOST.

    The NONCE is the real authority -- it proves the caller read the gateway
    host's keystone file. This check just refuses the obviously-remote shape
    early, and is knowingly imperfect behind same-host proxies, which is exactly
    why it is not the boundary. Mirrors the update step-up's
    ``_loopback_peer``: an AF_UNIX caller has an EMPTY ``request.remote``, so an
    IP-only test would 403 the CLI's preferred transport (the unix socket, whose
    SO_PEERCRED check is stronger host-locality evidence than any IP), and the
    auth middleware's ``internal_auth`` / ``peer_verified`` marks pass a
    local-secret-authenticated caller however it connected.
    """
    if request.get("internal_auth") or request.get("peer_verified"):
        return True
    from kiro_crew.dashboard.origin import request_is_unix_socket
    from kiro_crew.dashboard.urls import is_loopback

    if request_is_unix_socket(request):
        return True
    return is_loopback(request.remote or "")


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER on every consent endpoint.

    Async because the denial is AUDITED, and the audit writes to the security event
    log -- a synchronous disk write, whose first call on a fresh gateway also
    initialises the log. Running that on the event loop would let one refused
    request stall every other request and the heartbeat, so it goes through
    ``asyncio.to_thread`` (``no-blocking-call-on-event-loop``). The owner PREDICATE
    itself is pure and stays inline.
    """
    if is_owner_dashboard_request(request):
        return None
    # Names the calling APP, never a credential -- worded to say so plainly, since
    # "token" in a logger literal reads as a possible secret to the SAST rule.
    logger.warning(
        "refused %s: confirming delivery of scanner-flagged files is a dashboard "
        "owner action (app=%s)",
        operation,
        request.get("app"),
    )
    await asyncio.to_thread(
        file_delivery_consent.audit_decision,
        "*",
        outcome="denied",
        detail=f"{operation}: non-owner caller refused",
    )
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


def _requested_class(request: web.Request) -> str | None:
    """The grantable destination class named by the query, or ``None``.

    Validated against ``GRANTABLE_CLASSES`` rather than parsed loosely, so a
    request naming one of the never-grantable legs (the Slack or channel upload
    path) is refused here as an unknown class and never reaches the store.
    """
    requested = (request.query.get("destination_class") or "").strip()
    return requested if requested in file_delivery_consent.GRANTABLE_CLASSES else None


def _grant_payload(grant: file_delivery_consent.Grant | None) -> dict[str, object] | None:
    if grant is None:
        return None
    return grant.to_dict()


async def api_file_delivery_consent_get(request: web.Request) -> web.Response:
    """GET /api/file-delivery/consent -- the grantable classes and their consent."""
    denied = await _deny_non_owner(request, "file_delivery_consent.read")
    if denied:
        return denied
    grants = {
        name: _grant_payload(await asyncio.to_thread(file_delivery_consent.read_grant, name))
        for name in sorted(file_delivery_consent.GRANTABLE_CLASSES)
    }
    return web.json_response(
        {
            "ok": True,
            "grantable": sorted(file_delivery_consent.GRANTABLE_CLASSES),
            # Surfaced so the settings panel can SAY that the upload legs are
            # permanently excluded rather than merely omitting them, which reads
            # as an oversight.
            "never_grantable": sorted(file_delivery_consent.NEVER_GRANTABLE_CLASSES),
            "labels": file_delivery_consent.CLASS_LABELS,
            "grants": grants,
        }
    )


async def api_file_delivery_consent_post(request: web.Request) -> web.Response:
    """POST /api/file-delivery/consent -- ARM a grant, do not record it.

    Recording is split so an owner-authenticated but agent-DRIVEN browser cannot
    self-grant: consent must be a human dashboard action, not something a
    background agent completes on the owner's behalf. This endpoint only arms: it
    writes a single-use approval nonce to a
    keystone file the SPA can never read and returns the request id plus the
    host command that finishes the grant. The grant is recorded by
    :func:`api_file_delivery_consent_approve` once that command presents the
    nonce -- proof of host presence an agent-driven browser cannot fake.
    """
    denied = await _deny_non_owner(request, "file_delivery_consent.arm")
    if denied:
        return denied
    destination_class = _requested_class(request)
    if destination_class is None:
        return web.json_response(
            {"error": "unknown destination class", "code": _CODE_UNKNOWN_CLASS}, status=400
        )
    try:
        pending = await asyncio.to_thread(
            file_delivery_consent.arm_grant, destination_class, source="dashboard"
        )
    except file_delivery_consent.StepUpError as exc:
        return web.json_response({"error": str(exc), "code": _CODE_ARM_FAILED}, status=500)
    return web.json_response({"ok": True, **file_delivery_consent.public_pending_view(pending)})


async def api_file_delivery_consent_arm_status(request: web.Request) -> web.Response:
    """GET /api/file-delivery/consent/arm -- the armed request, SPA-safe.

    Owner-gated like every other verb: the armed request names which destination
    a grant is pending for, which is the same reconnaissance the read gate fences.
    """
    denied = await _deny_non_owner(request, "file_delivery_consent.arm_status")
    if denied:
        return denied
    pending = await asyncio.to_thread(file_delivery_consent.read_pending_grant)
    return web.json_response(file_delivery_consent.public_pending_view(pending))


def _live_agent_pids(request: web.Request) -> list[int]:
    """Host pids of every currently registered agent session.

    Read off the session registry rather than by scanning processes, because the
    question is "which agents does THIS gateway have running", and the registry
    is the only answer that cannot include somebody else's process.

    Fails CLOSED as a sentinel list: if the registry cannot be read at all, this
    returns a single invalid pid so the caller's confinement check refuses,
    rather than an empty list that would read as "no agent is running".
    """
    try:
        state = request.app["state"]
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return [-1]
        pids: list[int] = []
        for key in sessions.session_keys():
            pid = sessions.get_pid(key)
            if isinstance(pid, int) and not isinstance(pid, bool):
                pids.append(pid)
        return pids
    except Exception:  # noqa: BLE001 -- an unreadable registry is not "nothing running"
        logger.warning("file-delivery approve: could not enumerate agent pids", exc_info=True)
        return [-1]


async def _refuse_approve(detail: str, *, error: str, code: str) -> web.Response:
    """Audit an approve-path authorization denial, then answer 403.

    Every leg that refuses to record a grant goes through here, for the reason
    the non-owner refusal already audits: a denial that leaves no SEL entry is a
    denial an incident review cannot see, and these are exactly the attempts a
    review asks about -- an approve arriving from off-host, one arriving while
    computer use or an unsandboxed agent has voided the presence proof, and one
    presenting a nonce that is wrong or has expired. ``audit_decision`` never
    raises, so an audit failure cannot be what stops a refusal from being
    enforced.

    ``"*"`` as the destination class matches the non-owner refusal: the denial
    happens before any pending request is validated, so there is no class to
    name, and inventing one would put an unvalidated caller's word in the audit.
    """
    await asyncio.to_thread(
        file_delivery_consent.audit_decision, "*", outcome="denied", detail=detail
    )
    return web.json_response({"error": error, "code": code}, status=403)


async def api_file_delivery_consent_approve(request: web.Request) -> web.Response:
    """POST /api/file-delivery/consent/approve -- consume the nonce, record the grant.

    Called by ``kirocrew file-delivery approve`` on the gateway host, which read
    the nonce from the keystone data home. Loopback/unix-socket only: the proof of
    authority is host-locality plus possession of the owner-armed nonce, NOT a
    dashboard session, so this endpoint is deliberately not reachable from a remote
    bearer. It is the one place a grant is written.
    """
    if not _approve_is_local(request):
        return await _refuse_approve(
            "approve: non-local caller refused",
            error="approval is accepted from the gateway host only",
            code=_CODE_APPROVE_NOT_LOCAL,
        )
    # Exclude the one tier that makes this step-up synthesizable by the agent.
    # The host step-up proves a human is at the machine ONLY because a confined
    # agent cannot type into a host terminal -- UNLESS computer use is enabled,
    # which is exactly desktop observation + input synthesis. With it on, a
    # prompt-injected agent could type `kirocrew file-delivery approve` into an
    # existing terminal and complete the owner's armed step-up itself. So while
    # computer use is enabled we refuse to record the grant here (the one place a
    # grant is written): the presence proof is not trustworthy in that config.
    # Reads the keystone `computer_use.json`, which fails soft to DISABLED, so a
    # missing/corrupt file leaves the step-up usable -- the safe direction, since
    # disabled computer use is precisely when the terminal proof holds.
    from kiro_crew.computer_use import enable_state as _cu_enable_state

    if await asyncio.to_thread(_cu_enable_state.is_enabled):
        return await _refuse_approve(
            "approve: refused while computer use is enabled",
            error=(
                "computer use is enabled, so a host terminal step-up can no longer "
                "prove a human is present; disable computer use in Settings > "
                "Computer Use, then arm and approve delivery again"
            ),
            code=_CODE_APPROVE_COMPUTER_USE,
        )
    # Exclude the other tier that voids the presence proof: an UNSANDBOXED agent.
    # The nonce's secrecy from the agent rests on the pending-nonce leaf being
    # bind-masked out of the agent sandbox. If the effective sandbox would NOT
    # apply that mask -- agent.sandbox is 'off', or no backend is available and
    # unsandboxed exec is opted in -- an unsandboxed agent can read (or forge) the
    # nonce and the local secret directly, then drive the loopback approve with no
    # human present. So refuse to record a grant unless the mask will actually be
    # applied. `credential_mask_applies` is the same security-reviewed predicate
    # the ACP tool gate uses for exactly this "my control depends on the mask"
    # question, and it fails CLOSED (no backend -> False -> refuse).
    from kiro_crew.sandbox import (
        configured_sandbox_mode,
        credential_mask_applies,
        spawn_delegates_masking,
        unconfined_live_agent_pid,
    )

    # TWO questions, not one. `credential_mask_applies` answers whether the
    # backend `wrap_argv` selects would CARRY the mask; `spawn_delegates_masking`
    # answers whether that wrap happens at all. On macOS with kiro-cli's internal
    # sandbox enabled a backend is present, so the first answers True while the
    # spawn is handed to kiro-cli and Crew's mask never runs -- an agent could
    # then read the nonce and complete the owner's step-up with no human present.
    # Both must hold, and both fail closed.
    if not await asyncio.to_thread(lambda: credential_mask_applies(configured_sandbox_mode())):
        return await _refuse_approve(
            "approve: refused because the nonce-hiding sandbox mask is not in effect",
            error=(
                "the agent sandbox that hides this approval's nonce is not in "
                "effect (agent.sandbox is 'off', or no sandbox backend is "
                "available), so a host step-up can no longer prove a human is "
                "present; set agent.sandbox to 'standard' or 'strict' on a host "
                "with a working backend, then arm and approve delivery again"
            ),
            code=_CODE_APPROVE_UNSANDBOXED,
        )
    # The two checks above read CONFIGURATION, which describes the next spawn.
    # `agent.sandbox` is a live setting, so a session spawned while the tier was
    # `off` is still running unconfined after the config flips -- and that live
    # agent can read the nonce even though every config answer now says it
    # cannot. Ask the processes, not the config. Unreadable counts as unconfined.
    live_pids = await asyncio.to_thread(_live_agent_pids, request)
    stale = await asyncio.to_thread(unconfined_live_agent_pid, live_pids)
    if stale is not None:
        return await _refuse_approve(
            "approve: refused while a live agent session is not confined",
            error=(
                "an agent session is still running without the confinement that "
                "hides this approval's nonce (it started before the current "
                "sandbox setting), so a host step-up can no longer prove a human "
                "is present; stop that session or restart the gateway, then arm "
                "and approve delivery again"
            ),
            code=_CODE_APPROVE_UNSANDBOXED,
        )
    if await asyncio.to_thread(spawn_delegates_masking):
        return await _refuse_approve(
            "approve: refused because agent spawns delegate masking to another sandbox",
            error=(
                "this host delegates agent confinement to another sandbox, so the "
                "mask that hides this approval's nonce from the agent never runs "
                "and a host step-up can no longer prove a human is present; "
                "disable kiro-cli's internal sandbox so Crew's own sandbox applies, "
                "then arm and approve delivery again"
            ),
            code=_CODE_APPROVE_UNSANDBOXED,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("nonce"), str):
        return web.json_response(
            {"error": "nonce must be a string", "code": _CODE_INVALID_NONCE}, status=400
        )
    try:
        pending = await asyncio.to_thread(file_delivery_consent.claim_grant, body["nonce"])
    except file_delivery_consent.StepUpError as exc:
        # The refusal reason, not the nonce: the caller's nonce is a credential
        # and the message names why validation failed (wrong or expired), which
        # is what a review needs.
        return await _refuse_approve(
            f"approve: step-up refused ({exc})",
            error=str(exc),
            code=_CODE_APPROVE_REFUSED,
        )
    # The nonce is CLAIMED, so single-use is already decided and no second
    # approval can hold this request while the grant is written. A failed write
    # stays non-destructive by handing the request back -- but only while nothing
    # newer has been armed, since overwriting a newer request would delete one
    # nobody approved.
    granted_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        grant = await asyncio.to_thread(
            file_delivery_consent.record_grant, pending.destination_class, granted_at=granted_at
        )
    except OSError as exc:
        restored = await asyncio.to_thread(file_delivery_consent.restore_pending_grant, pending)
        logging.getLogger(__name__).warning(
            "file-delivery grant write failed; armed request %s: %s",
            "restored for retry" if restored else "not restored (a newer request exists)",
            exc,
        )
        return web.json_response(
            {
                "error": (
                    "could not record the grant (a storage write failed); the approval "
                    "request is still armed, so run `kirocrew file-delivery approve` again"
                    if restored
                    else "could not record the grant (a storage write failed), and the "
                    "approval request could not be re-armed; confirm from the "
                    "dashboard's Security panel again"
                ),
                "code": _CODE_APPROVE_WRITE_FAILED,
            },
            status=500,
        )
    return web.json_response({"ok": True, "grant": grant.to_dict()})


async def api_file_delivery_consent_delete(request: web.Request) -> web.Response:
    """DELETE /api/file-delivery/consent -- withdraw a recorded confirmation."""
    denied = await _deny_non_owner(request, "file_delivery_consent.revoke")
    if denied:
        return denied
    destination_class = _requested_class(request)
    if destination_class is None:
        return web.json_response(
            {"error": "unknown destination class", "code": _CODE_UNKNOWN_CLASS}, status=400
        )
    removed = await asyncio.to_thread(file_delivery_consent.revoke, destination_class)
    return web.json_response({"ok": True, "removed": removed})

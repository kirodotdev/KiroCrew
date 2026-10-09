"""Owner-gated dashboard endpoints for the ``SSH_AUTH_SOCK`` forward consent.

The ONLY writer of ``ssh_auth_sock_consent.json`` in the tree (a hand edit from
outside the sandbox still works and reads identically). The store sits on the
keystone floor so an agent cannot write it with file tools or a shell form.
Recording a grant takes TWO acts here, the exact shape of
``handlers/file_delivery_consent.py``: the owner-gated POST ARMS a request
(writing a single-use nonce to a sandbox-hidden leaf the SPA can never read), and
the loopback-only approve endpoint -- driven by ``kirocrew ssh-agent approve`` on
the gateway host -- consumes that nonce and records the grant. The CLI verb
authorizes nothing on its own; it proves host presence by reading a nonce an
automated caller cannot, which closes the "owner-authenticated but agent-DRIVEN
browser self-grants" hole an owner-session identity check alone leaves open.

Every read/arm/revoke verb is refused to anyone but the dashboard OWNER, for the
three callers the file-delivery handler names (an agent via an app token, an app
token with no human in the loop, an allow-listed messaging non-owner running
``!dashboard``). Reads are refused too: the response says whether the owner's
ssh-agent is reachable from inside a session, which is reconnaissance.

Routes and their shapes (the SPA's contract):

* ``GET    /api/ssh-agent/consent``          -> ``{granted, granted_at, socket_present}``
* ``POST   /api/ssh-agent/consent/arm``      -> ``{request_id, expires_in, approve_command}``
* ``GET    /api/ssh-agent/consent/arm``      -> ``{armed, request_id, expires_in, approve_command}``
* ``POST   /api/ssh-agent/consent/approve``  -> ``{granted: true, granted_at}`` (host only;
  403 off-host / computer use / unconfined / epoch moved, 409 bad or expired nonce)
* ``DELETE /api/ssh-agent/consent``          -> ``{granted: false}`` (revokes AND discards any armed request)
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from aiohttp import web

from kiro_crew import ssh_auth_sock_consent
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.file_delivery_consent import (
    _approve_is_local,
    _live_agent_pids,
)
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_ARM_FAILED = "ssh_agent_arm_failed"
_CODE_APPROVE_NOT_LOCAL = "ssh_agent_approve_not_local"
_CODE_APPROVE_REFUSED = "ssh_agent_approve_refused"
_CODE_APPROVE_COMPUTER_USE = "ssh_agent_approve_computer_use_active"
_CODE_APPROVE_UNSANDBOXED = "ssh_agent_approve_unsandboxed"
_CODE_APPROVE_WRITE_FAILED = "ssh_agent_approve_write_failed"
_CODE_INVALID_NONCE = "invalid_nonce"
_CODE_STALE_NONCE = "ssh_agent_nonce_stale"
_CODE_WITHDRAW_FAILED = "ssh_agent_withdraw_failed"


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER on every consent endpoint.

    Async because the denial is AUDITED, and the audit is a synchronous disk write
    that goes through ``asyncio.to_thread`` so one refused request cannot stall
    the event loop. The owner PREDICATE itself is pure and stays inline.
    """
    if is_owner_dashboard_request(request):
        return None
    logger.warning(
        "refused %s: forwarding the ssh-agent socket into sessions is a dashboard "
        "owner action (app=%s)",
        operation,
        request.get("app"),
    )
    await asyncio.to_thread(
        ssh_auth_sock_consent.audit_decision,
        outcome="refused",
        detail=f"{operation}: non-owner caller refused",
    )
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


def _consent_view() -> dict[str, object]:
    grant = ssh_auth_sock_consent.read_grant()
    return {
        "granted": grant is not None,
        "granted_at": (grant.granted_at or None) if grant is not None else None,
        "socket_present": ssh_auth_sock_consent.socket_present(),
    }


async def api_ssh_agent_consent_get(request: web.Request) -> web.Response:
    """GET /api/ssh-agent/consent -- the grant and whether there is a socket to forward."""
    denied = await _deny_non_owner(request, f"{ssh_auth_sock_consent.AUDIT_EVENT}.read")
    if denied:
        return denied
    return web.json_response(await asyncio.to_thread(_consent_view))


async def api_ssh_agent_consent_arm(request: web.Request) -> web.Response:
    """POST /api/ssh-agent/consent/arm -- ARM a grant, do not record it.

    Writes a single-use approval nonce to a sandbox-hidden leaf the SPA can never
    read and returns the request id plus the host command that finishes the
    grant. The grant is recorded by :func:`api_ssh_agent_consent_approve` once
    that command presents the nonce.
    """
    denied = await _deny_non_owner(request, f"{ssh_auth_sock_consent.AUDIT_EVENT}.arm")
    if denied:
        return denied
    try:
        pending = await asyncio.to_thread(ssh_auth_sock_consent.arm_grant)
    except ssh_auth_sock_consent.StepUpError as exc:
        return web.json_response({"error": str(exc), "code": _CODE_ARM_FAILED}, status=500)
    return web.json_response(
        {
            "request_id": pending.request_id,
            "expires_in": pending.expires_in,
            "approve_command": ssh_auth_sock_consent.APPROVE_COMMAND,
        }
    )


async def api_ssh_agent_consent_arm_status(request: web.Request) -> web.Response:
    """GET /api/ssh-agent/consent/arm -- the armed request, SPA-safe (no nonce)."""
    denied = await _deny_non_owner(request, f"{ssh_auth_sock_consent.AUDIT_EVENT}.arm_status")
    if denied:
        return denied
    pending = await asyncio.to_thread(ssh_auth_sock_consent.read_pending_grant)
    return web.json_response(ssh_auth_sock_consent.public_pending_view(pending))


async def _refuse_approve(detail: str, *, error: str, code: str) -> web.Response:
    """Audit an approve-path refusal, then answer 403.

    A denial that leaves no SEL entry is a denial an incident review cannot see,
    and these are exactly the attempts a review asks about. ``audit_decision``
    never raises, so an audit failure cannot be what stops a refusal from being
    enforced.
    """
    await asyncio.to_thread(ssh_auth_sock_consent.audit_decision, outcome="refused", detail=detail)
    return web.json_response({"error": error, "code": code}, status=403)


async def api_ssh_agent_consent_approve(request: web.Request) -> web.Response:
    """POST /api/ssh-agent/consent/approve -- consume the nonce, record the grant.

    Called by ``kirocrew ssh-agent approve`` on the gateway host. Loopback/unix-
    socket only: the proof of authority is host-locality plus possession of the
    owner-armed nonce, NOT a dashboard session. The one place a grant is written.

    The fences are the file-delivery approve's, for the same reasons given there
    in full: the host step-up proves a human is present only while the agent
    cannot read the nonce, so computer use (the agent can type the command), a
    sandbox whose mask does not apply, a spawn that delegates masking, and a
    live session still running unconfined each void the proof and refuse.
    """
    if not _approve_is_local(request):
        return await _refuse_approve(
            "approve: non-local caller refused",
            error="approval is accepted from the gateway host only",
            code=_CODE_APPROVE_NOT_LOCAL,
        )
    from kiro_crew.computer_use import enable_state as _cu_enable_state

    if await asyncio.to_thread(_cu_enable_state.is_enabled):
        return await _refuse_approve(
            "approve: refused while computer use is enabled",
            error=(
                "computer use is enabled, so a host terminal step-up can no longer "
                "prove a human is present; disable computer use in Settings > "
                "Computer Use, then allow SSH agent forwarding again"
            ),
            code=_CODE_APPROVE_COMPUTER_USE,
        )
    from kiro_crew.sandbox import (
        configured_sandbox_mode,
        credential_mask_applies,
        spawn_delegates_masking,
        unconfined_live_agent_pid,
    )

    if not await asyncio.to_thread(lambda: credential_mask_applies(configured_sandbox_mode())):
        return await _refuse_approve(
            "approve: refused because the nonce-hiding sandbox mask is not in effect",
            error=(
                "the agent sandbox that hides this approval's nonce is not in "
                "effect (agent.sandbox is 'off', or no sandbox backend is "
                "available), so a host step-up can no longer prove a human is "
                "present; set agent.sandbox to 'auto' or 'strict' on a host "
                "with a working backend, then allow SSH agent forwarding again"
            ),
            code=_CODE_APPROVE_UNSANDBOXED,
        )
    live_pids = await asyncio.to_thread(_live_agent_pids, request)
    stale = await asyncio.to_thread(unconfined_live_agent_pid, live_pids)
    if stale is not None:
        return await _refuse_approve(
            "approve: refused while a live agent session is not confined",
            error=(
                "an agent session is still running without the confinement that "
                "hides this approval's nonce (it started before the current "
                "sandbox setting), so a host step-up can no longer prove a human "
                "is present; stop that session or restart the gateway, then allow "
                "SSH agent forwarding again"
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
                "then allow SSH agent forwarding again"
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
    # Claim AND persist as ONE transaction (``claim_and_record``, under the module's
    # transaction lock), so the owner's DELETE cannot slip between the two and be
    # silently undone by the write that follows.
    granted_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        grant = await asyncio.to_thread(
            ssh_auth_sock_consent.claim_and_record, body["nonce"], granted_at=granted_at
        )
    except ssh_auth_sock_consent.StaleNonceError as exc:
        # No live request matches: absent, expired, wrong or already consumed.
        # 409 rather than 403 because the remedy is "arm again", not "you may
        # not" -- the SPA and the CLI both say so. Audited like every refusal;
        # the detail carries the reason, never the nonce (a credential).
        await asyncio.to_thread(
            ssh_auth_sock_consent.audit_decision,
            outcome="refused",
            detail=f"approve: stale nonce ({exc})",
        )
        return web.json_response({"error": str(exc), "code": _CODE_STALE_NONCE}, status=409)
    except ssh_auth_sock_consent.StepUpError as exc:
        # The safety epoch moved since the owner armed: an authorization refusal.
        return await _refuse_approve(
            f"approve: step-up refused ({exc})",
            error=str(exc),
            code=_CODE_APPROVE_REFUSED,
        )
    except ssh_auth_sock_consent.GrantWriteFailed as exc:
        # The nonce was CLAIMED (single-use already decided) and the store write
        # failed. The transaction handed the request back when nothing newer had
        # been armed; ``restored`` says which remedy to name.
        restored = exc.restored
        logger.warning(
            "ssh-agent consent write failed; armed request %s: %s",
            "restored for retry" if restored else "not restored (a newer request exists)",
            exc.cause,
        )
        return web.json_response(
            {
                "error": (
                    "could not record the grant (a storage write failed); the approval "
                    "request is still armed, so run `kirocrew ssh-agent approve` again"
                    if restored
                    else "could not record the grant (a storage write failed), and the "
                    "approval request could not be re-armed; allow SSH agent "
                    "forwarding from the dashboard's Security panel again"
                ),
                "code": _CODE_APPROVE_WRITE_FAILED,
            },
            status=500,
        )
    return web.json_response({"granted": True, "granted_at": grant.granted_at})


async def api_ssh_agent_consent_delete(request: web.Request) -> web.Response:
    """DELETE /api/ssh-agent/consent -- withdraw the grant AND any armed request.

    No step-up: fail-safe direction. The panel's Cancel (in the armed block) and
    Revoke (once granted) are this one verb, so both a recorded grant and a
    pending nonce are cleared; answers ``{"granted": false}`` either way, 200
    even when nothing was recorded or armed. A removed grant audits ``revoked``
    (inside ``revoke``); a discarded arm audits ``cancelled`` (inside
    ``discard_pending_grant``) and is logged at info here.
    """
    denied = await _deny_non_owner(request, f"{ssh_auth_sock_consent.AUDIT_EVENT}.revoke")
    if denied:
        return denied
    # One transaction under the module's lock, so an approve in flight is either
    # finished (and revoked here) or finds its nonce gone -- never half-done.
    try:
        revoked, discarded = await asyncio.to_thread(ssh_auth_sock_consent.withdraw)
    except OSError as exc:
        # The store or the nonce could not be written/removed. Say so: a 200 here
        # would tell the owner the cancel took while the request stays
        # approvable until it expires (persist-before-you-publish).
        logger.warning("ssh-agent consent withdraw failed: %s", exc)
        await asyncio.to_thread(
            ssh_auth_sock_consent.audit_decision,
            outcome="refused",
            detail=f"withdraw: storage error ({exc.__class__.__name__})",
        )
        return web.json_response(
            {
                "error": (
                    "could not withdraw the request (a storage write failed); "
                    "the request may still be pending -- try Cancel or Revoke again"
                ),
                "code": _CODE_WITHDRAW_FAILED,
            },
            status=500,
        )
    if discarded and not revoked:
        logger.info("ssh-agent consent: armed request cancelled by the owner before approval")
    return web.json_response({"granted": False})

"""Paid-AWS-service consent REST API — the confirmation surface.

``GET    /api/aws/consent?service=<id>``  what would be billed, and whether confirmed
``POST   /api/aws/consent``               record the operator's confirmation
``DELETE /api/aws/consent?service=<id>``  withdraw it

This handler is the operator's own out-of-band control surface, and being the
only writer (with the ``kirocrew aws-consent`` CLI) is what makes "the agent
cannot consent to spending the operator's money" true: the grant lives on the
keystone floor, which the agent can neither read nor write, and this handler
opens that path directly rather than through the agent tool gate.

The GET is deliberately the side that performs the ``sts:GetCallerIdentity``
probe. It is free, non-mutating, and it is the whole point of the surface --
showing the operator the account, region and credential source BEFORE they
agree. It is also where account drift is caught: if a grant was recorded for one
account and the profile now resolves to another, the probe revokes the stale
grant here, so the next synthesis refuses and the operator is asked again.

Blocking work is offloaded. The keystone read/write touches the filesystem and
the identity probe spawns the AWS CLI, so neither may run on the event loop.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
    stale_owner_session_response,
)

logger = logging.getLogger(__name__)

#: Machine-readable error codes, per the dashboard error-code contract
#: (``test/test_error_code_contract.py``): a client must be able to branch on the
#: failure without parsing English prose.
_CODE_UNKNOWN_SERVICE = "unknown_aws_service"
_CODE_INVALID_JSON = "invalid_json"
_CODE_IDENTITY_UNRESOLVED = "aws_identity_unresolved"
_CODE_STALE_CONFIRMATION = "aws_consent_stale_confirmation"
_CODE_OWNER_REQUIRED = "dashboard_owner_required"


def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER on every consent endpoint.

    Confirming a charge spends the owner's money, so it is an owner action --
    narrower than "authenticated" and narrower than "not an app". Two callers had
    to be shut out, and only the second is obvious:

    * an APP token: an app declaring the ``/api/aws/consent`` permission would
      otherwise mint a grant with no human in the loop -- the keystone stops the
      agent WRITING the file and the CLI verb was removed, so this was the same
      door's third key.
    * an allowed MESSAGING user: a Slack allow-listed non-owner who runs
      ``!dashboard`` authenticates with ``app == ""``, so an app-only check let
      them through to authorize spending in the OWNER's AWS account.

    ``is_owner_dashboard_request`` already encodes exactly that rule (app must be
    present and empty, caller must equal ``owner_id`` or be a local-owner
    subject), so it is reused rather than re-derived -- the same reason
    ``ask_question`` and ``mcp_apps`` reuse it. Reads are refused too: the GET
    names the account id and caller ARN that a keystone read is fenced from.
    Both findings came from review.
    """
    if is_owner_dashboard_request(request):
        return None
    # Names the calling APP, never a credential -- worded to say so plainly, since
    # "token" in a logger literal reads as a possible secret to the SAST rule.
    logger.warning(
        "refused %s: confirming AWS charges is a dashboard owner action (app=%s)",
        operation,
        request.get("app"),
    )
    aws_consent.audit_decision(
        "*", outcome="denied", detail=f"{operation}: non-owner caller refused"
    )
    # Deny decision made above; only the response label changes for a signed
    # pre-owner bootstrap subject (see stale_owner_session_response).
    stale = stale_owner_session_response(request)
    if stale is not None:
        return stale
    return web.json_response(
        {"error": "dashboard owner required", "code": _CODE_OWNER_REQUIRED}, status=403
    )


def _requested_service(request: web.Request) -> str | None:
    """The ``service`` query parameter, or None when it is not a gated one."""
    service = (request.rel_url.query.get("service") or "").strip()
    return service if service in aws_consent.GATED_SERVICES else None


async def _effective_target(service: str) -> tuple[str, str]:
    """The (profile, region) that ``service`` would actually use right now.

    Read from live config rather than taken from the request, so a confirmation
    can only ever be recorded against the settings the code will really use. A
    client-supplied profile/region would let the confirmation and the request
    disagree -- the operator would be shown one account and bill another.
    """
    if service == aws_consent.SERVICE_POLLY:
        from kiro_crew.slack.handler import _vc

        return _vc.aws_profile, _vc.region

    from kiro_crew.config.loader import KiroCrewConfig

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    return cfg.stt.transcribe_profile, cfg.stt.transcribe_region


def _grant_payload(grant: aws_consent.Grant | None) -> dict[str, object] | None:
    return grant.to_dict() if grant is not None else None


async def api_aws_consent_get(request: web.Request) -> web.Response:
    """GET /api/aws/consent — what this service would bill, and its consent."""
    denied = _deny_non_owner(request, "aws_consent.read")
    if denied:
        return denied
    service = _requested_service(request)
    if service is None:
        return web.json_response(
            {"error": "unknown service", "code": _CODE_UNKNOWN_SERVICE}, status=400
        )

    profile, region = await _effective_target(service)
    identity = await aws_consent.probe_identity(profile, region)

    # Drift check BEFORE reading the grant back, so a revoked-as-stale grant is
    # reported as absent in this same response instead of one request later.
    drifted = await asyncio.to_thread(aws_consent.reconcile_drift, service, identity)
    grant = await asyncio.to_thread(aws_consent.read_grant, service)
    granted, reason = await asyncio.to_thread(
        aws_consent.is_granted, service, profile=profile, region=region
    )

    return web.json_response(
        {
            "service": service,
            "serviceLabel": aws_consent.SERVICE_LABELS[service],
            "profile": profile,
            "credentialSource": aws_consent.credential_source(profile),
            "region": region,
            "account": identity.account,
            "arn": identity.arn,
            "identityResolved": identity.ok,
            "identityDetail": identity.detail,
            "granted": granted,
            "reason": reason,
            "revokedOnAccountChange": drifted,
            "grant": _grant_payload(grant),
        }
    )


async def api_aws_consent_post(request: web.Request) -> web.Response:
    """POST /api/aws/consent — record the operator's confirmation.

    The body must echo the profile, region and account the UI DISPLAYED, and all
    three must still match. Reading them from live config alone was not enough:
    the consent card and the provider fields are separate queries, so an operator
    could be shown account A, change the profile, and then confirm -- recording
    account B while A was still on screen. Confirming what you were shown is the
    whole point of this surface, so a mismatch is a 409 and the operator re-reads
    it (found in review).

    Also refuses when the account cannot be resolved. That is the issue's "if
    confirmation is unavailable, no request should be made" clause taken
    literally at the only place it can be honoured: a confirmation that cannot
    name the account it is confirming is not informed consent, so it is not
    recorded and the feature stays refused.
    """
    denied = _deny_non_owner(request, "aws_consent.grant")
    if denied:
        return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)

    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": _CODE_INVALID_JSON}, status=400
        )
    raw_service = body.get("service")
    if not isinstance(raw_service, str):
        # ``{"service": []}`` would otherwise reach ``list.strip()`` and 500.
        return web.json_response(
            {"error": "unknown service", "code": _CODE_UNKNOWN_SERVICE}, status=400
        )
    service = raw_service.strip()
    if service not in aws_consent.GATED_SERVICES:
        return web.json_response(
            {"error": "unknown service", "code": _CODE_UNKNOWN_SERVICE}, status=400
        )

    profile, region = await _effective_target(service)
    # Fresh probe, cache bypassed: the operator is agreeing to THIS account, so
    # the value recorded must not be one read from a window that opened earlier.
    identity = await aws_consent.probe_identity(profile, region, use_cache=False)
    if not identity.ok:
        return web.json_response(
            {
                "error": "could not resolve the AWS account, so nothing was confirmed",
                "code": _CODE_IDENTITY_UNRESOLVED,
                "identityDetail": identity.detail,
            },
            status=409,
        )

    shown = {
        "profile": str(body.get("expectedProfile", "")),
        "region": str(body.get("expectedRegion", "")),
        "account": str(body.get("expectedAccount", "")),
    }
    current = {"profile": profile, "region": region, "account": identity.account}
    if shown != current:
        return web.json_response(
            {
                "error": (
                    "the settings changed since they were shown, so nothing was "
                    "confirmed -- review the account and confirm again"
                ),
                "code": _CODE_STALE_CONFIRMATION,
                "shown": shown,
                "current": current,
            },
            status=409,
        )

    granted_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    grant = await asyncio.to_thread(
        aws_consent.record_grant,
        service,
        profile=profile,
        region=region,
        account=identity.account,
        arn=identity.arn,
        granted_at=granted_at,
    )
    logger.info(
        "operator confirmed %s use for account %s via %s",
        aws_consent.SERVICE_LABELS[service],
        identity.account,
        aws_consent.credential_source(profile),
    )
    return web.json_response({"ok": True, "grant": grant.to_dict()})


async def api_aws_consent_delete(request: web.Request) -> web.Response:
    """DELETE /api/aws/consent — withdraw a recorded confirmation."""
    denied = _deny_non_owner(request, "aws_consent.revoke")
    if denied:
        return denied
    service = _requested_service(request)
    if service is None:
        return web.json_response(
            {"error": "unknown service", "code": _CODE_UNKNOWN_SERVICE}, status=400
        )
    removed = await asyncio.to_thread(aws_consent.revoke, service)
    return web.json_response({"ok": True, "removed": removed})

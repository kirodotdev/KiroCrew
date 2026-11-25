"""The Slack owner-claim routes: ``GET /api/slack/owner-claim`` and
``POST /api/slack/owner-claim/start``, ``/cancel`` and ``/confirm``.

Thin HTTP layer over :class:`kiro_crew.slack.owner_claim.OwnerClaim`, which
holds the window, the candidates and the compare-then-write. Every route is
direct-local only, the GET included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _sel,
        _write_env_or_roll_back,
        body_not_object_error,
        is_direct_local_request,
        record_milestone_when,
    )
    from kiro_crew.slack.owner_claim import OwnerClaim


def _owner_claim_gate(request: web.Request, operation: str) -> OwnerClaim | web.Response:
    """The gateway's claim window, or the refusal: remote 403, no claim window here 503.

    Every owner-claim route is direct-local only, the GET included: the
    candidates it lists (names, text) must not reach a remote viewer.
    """
    caller = request.get("user", "dashboard")
    if not is_direct_local_request(request):
        code, msg, status = "remote_read_only", "local machine only", 403
    elif getattr(request.app["state"], "slack_owner_claim", None) is None:
        code, msg, status = "slack_reconnect_unavailable", "no Slack connection here", 503
    else:
        claim: OwnerClaim = request.app["state"].slack_owner_claim
        return claim
    _sel().log_api_access(
        caller=caller, operation=operation, outcome="denied", source="dashboard", error=code
    )
    return web.json_response({"error": msg, "code": code}, status=status)


def _owner_claim_refused(exc: Exception) -> web.Response:
    code = str(getattr(exc, "code", "refused"))
    return web.json_response({"error": code, "code": code}, status=getattr(exc, "status", 409))


async def api_slack_owner_claim_get(request: web.Request) -> web.Response:
    """GET /api/slack/owner-claim — the window and its candidates. Direct-local only."""
    claim = _owner_claim_gate(request, "slack.owner_claim.read")
    if isinstance(claim, web.Response):
        return claim
    return web.json_response(claim.snapshot())


async def api_slack_owner_claim_start(request: web.Request) -> web.Response:
    """POST /api/slack/owner-claim/start — open the window and connect in claim mode."""
    from kiro_crew.slack.owner_claim import ClaimRefused
    from kiro_crew.slack.reconnect import CredentialStoreUnreadable

    claim = _owner_claim_gate(request, "slack.owner_claim.start")
    if isinstance(claim, web.Response):
        return claim
    try:
        body = await claim.start()
    except ClaimRefused as exc:
        return _owner_claim_refused(exc)
    except CredentialStoreUnreadable:
        code = "credential_store_unreadable"
        return web.json_response({"error": code, "code": code}, status=500)
    return web.json_response(body)


async def api_slack_owner_claim_cancel(request: web.Request) -> web.Response:
    """POST /api/slack/owner-claim/cancel — close the window and disconnect."""
    claim = _owner_claim_gate(request, "slack.owner_claim.cancel")
    if isinstance(claim, web.Response):
        return claim
    await claim.close("cancelled")
    return web.json_response(claim.snapshot())


async def api_slack_owner_claim_confirm(request: web.Request) -> web.Response:
    """POST /api/slack/owner-claim/confirm ``{"user_id"}`` — make that candidate the owner.

    Compare-then-write under ``_get_config_lock()`` (inside ``confirm``), then
    the ordinary Reconnect, which accepts a first owner.
    """
    from kiro_crew.config.loader import CRED_OWNER_ID
    from kiro_crew.slack.owner_claim import ClaimRefused
    from kiro_crew.slack.reconnect import CredentialStoreUnreadable
    from kiro_crew.validation import USER_ID_RE

    claim = _owner_claim_gate(request, "slack.owner_claim.confirm")
    if isinstance(claim, web.Response):
        return claim
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    user_id = body.get("user_id")
    if not isinstance(user_id, str) or not USER_ID_RE.match(user_id):
        return web.json_response({"error": "user_id", "code": "invalid_user_id"}, status=400)

    async def _write_owner(value: str, check: Callable[[str], None]) -> None:
        await _write_env_or_roll_back({CRED_OWNER_ID: value}, None, check=check)

    state = request.app["state"]
    try:
        result = await claim.confirm(
            user_id,
            write_owner=_write_owner,
            reconnect=getattr(state, "slack_reconnect", None),
        )
    except ClaimRefused as exc:
        return _owner_claim_refused(exc)
    except CredentialStoreUnreadable:
        code = "credential_store_unreadable"
        return web.json_response({"error": code, "code": code}, status=500)
    record_milestone_when("slack", "channel_connected:slack")
    return web.json_response(result)

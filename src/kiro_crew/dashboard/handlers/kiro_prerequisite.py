"""Authenticated dashboard handlers for Kiro CLI first-run setup."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.kiro_prerequisite import (
    KIRO_CLI_LOGIN_COMMAND,
    KIRO_CLI_SSO_LOGIN_COMMAND,
    OFFICIAL_INSTALL_DOCS_URL,
    KiroPrerequisiteService,
    PrerequisiteStatus,
    legacy_idle_operation,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)
_LOCAL_DASHBOARD_OWNER_SUBJECTS = frozenset({"local-app", "local-startup"})


def _not_ready_snapshot(initial_setup_complete: bool = False) -> dict[str, Any]:
    """A retryable not-ready snapshot for when a status probe cannot run.

    Shaped exactly like ``KiroPrerequisiteService.snapshot()`` (built from the
    same dataclasses so it cannot drift), it reports the CLI as installed but
    not signed in so the dashboard shows a retry path rather than a 500 flash.

    ``initial_setup_complete`` is carried through from the service so a failed
    probe does not demote a returning user to first-run — reporting ``False``
    here makes the SPA restore the full-screen first-run setup gate for someone
    who finished setup long ago.
    """

    result: dict[str, Any] = asdict(
        PrerequisiteStatus(
            platform="gateway",
            installed=True,
            initial_setup_complete=initial_setup_complete,
        )
    )
    # See _LEGACY_IDLE_OPERATION: a pre-upgrade tab crashes without this key.
    result["operation"] = legacy_idle_operation()
    return result


def _service(request: web.Request) -> KiroPrerequisiteService:
    service = request.app.get("kiro_prerequisite_service")
    if not isinstance(service, KiroPrerequisiteService):
        raise web.HTTPServiceUnavailable(
            text="Kiro prerequisite service unavailable.",
            content_type="text/plain",
        )
    return service


def _caller(request: web.Request) -> str:
    user = request.get("user", "")
    return str(user) if user else "dashboard-user"


def _is_dashboard_owner(request: web.Request) -> bool:
    """Return whether a signed dashboard identity may operate host setup."""

    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    return request.get("app") == "" and (
        (owner_id and caller == owner_id)
        or (not owner_id and caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS)
    )


async def _dashboard_owner_only(request: web.Request) -> web.Response | None:
    """Require the configured owner or a signed standalone-local identity."""

    if _is_dashboard_owner(request):
        return None

    caller = str(request.get("user") or "")
    audit_caller = str(request.get("app") or caller or "unknown")

    def _audit() -> None:
        sel().log_api_access(
            caller=audit_caller,
            operation="kiro_prerequisite_access",
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="dashboard owner required",
        )

    try:
        await asyncio.to_thread(_audit)
    except Exception:
        logger.debug("Could not audit denied Kiro prerequisite access", exc_info=True)
    return web.json_response({"error": "dashboard owner required"}, status=403)


async def api_kiro_prerequisite_status(request: web.Request) -> web.Response:
    """GET /api/kiro-prerequisite — current install/login readiness.

    Reads LATCHED state by default: readiness is probed at gateway start and
    then only on explicit request, so the SPA's background poll costs no
    ``kiro-cli`` subprocess. Two query modes force a real probe, and they are
    deliberately distinct: ``?refresh=explicit`` (also ``1``/``true``) is the
    HUMAN action (the gate's Check again) and always probes, while
    ``?refresh=auto`` is the blocking gate's machine poll, which is coalesced
    behind a short floor so several open tabs cannot multiply the spawns.
    """

    if request.get("app") != "":
        denied = await _dashboard_owner_only(request)
        assert denied is not None
        return denied

    # Only an owner may force a host probe; a non-owner's refresh reads latched
    # state like any other poll (they receive the redacted payload regardless).
    refresh = request.query.get("refresh")
    is_owner = _is_dashboard_owner(request)
    explicit = refresh in ("1", "true", "explicit") and is_owner
    auto = refresh == "auto" and is_owner

    # Resolve the service OUTSIDE the guard: a genuinely unwired service is a
    # real misconfiguration that must stay a 503, not be masked as a 200
    # not-ready. Only the probe itself is guarded.
    service = _service(request)
    try:
        snapshot = await service.snapshot(force=explicit or auto, coalesce=auto)
    except asyncio.CancelledError:
        raise
    except web.HTTPException:
        raise
    except Exception:
        # A transient probe failure must not surface as a 500 that flashes the
        # full-screen "could not check Kiro CLI" gate. Report a retryable
        # not-ready snapshot so the dashboard keeps polling. (The probe layer
        # already degrades most failures; this is the last-resort backstop.)
        # The first-run bit is read from the data home, not the probe, so it
        # survives this path and keeps a returning user out of first-run setup.
        logger.warning("Kiro prerequisite status probe failed", exc_info=True)
        snapshot = _not_ready_snapshot(bool(service.initial_setup_complete))
    if _is_dashboard_owner(request):
        return web.json_response({**snapshot, "setup_allowed": True})

    # Authorized non-owner dashboard users need the readiness bit so the
    # application gate does not lock them out after the owner completes setup.
    # Do not expose the host platform, candidate state, operation output, URLs,
    # or mutations to those users.
    return web.json_response(
        {
            "platform": "gateway",
            "installed": False,
            "authenticated": False,
            "ready": bool(snapshot.get("ready")),
            "initial_setup_complete": bool(snapshot.get("initial_setup_complete")),
            "repair_required": False,
            "docs_url": OFFICIAL_INSTALL_DOCS_URL,
            "login_command": KIRO_CLI_LOGIN_COMMAND,
            "sso_login_command": KIRO_CLI_SSO_LOGIN_COMMAND,
            "setup_allowed": False,
            # Redacted like the rest of this block: the failure kind and probe
            # detail describe the HOST's sandbox posture (kernel knobs, errnos),
            # which is exactly the candidate/host state a non-owner must not see.
            # Kept present so the payload shape never varies by caller — a
            # non-owner already routes to the "owner must finish setup" screen.
            "sandbox_unavailable": False,
            "sandbox_failure_kind": "",
            "sandbox_detail": "",
            "sandbox_remedy": "",
            # Redacted for the same reason as the block above, and kept present
            # for the same shape-stability reason: only the owner can act on a
            # missing spec (the repair is an owner-gated POST). A non-owner on an
            # established install still gets the app -- the gate returns children
            # on ``initial_setup_complete`` before it consults these keys -- so
            # withholding them costs them nothing.
            "missing_agent_specs": [],
            "agent_spec_repair_error": "",
            # See _LEGACY_IDLE_OPERATION: a pre-upgrade tab crashes without this,
            # and the payload shape must not vary by caller either way.
            "operation": legacy_idle_operation(),
        }
    )


async def api_kiro_prerequisite_repair_specs(request: web.Request) -> web.Response:
    """POST /api/kiro-prerequisite/repair-specs — rewrite the managed agent specs.

    A POST rather than a flag on the status GET, because the write must be
    origin-checked and audited: ``csrf_middleware`` skips ``check_origin`` for
    ``{GET, HEAD, OPTIONS}`` and ``sel_audit_middleware`` logs only
    ``{POST, PUT, DELETE, PATCH}``, so hanging this off the status read would make
    a spec rewrite cross-site triggerable and invisible to the audit log.

    Returns 200 with the post-repair snapshot (unlike install/login's 202) because
    the repair runs to completion within the request — it is one bounded file
    write, not a long-lived background operation with progress to poll.
    """

    denied = await _dashboard_owner_only(request)
    if denied is not None:
        return denied
    snapshot = await _service(request).repair_agent_specs(_caller(request))
    return web.json_response({**snapshot, "setup_allowed": True})

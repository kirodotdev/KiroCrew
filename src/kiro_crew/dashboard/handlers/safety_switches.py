"""Dashboard handlers for the owner's MCP safety switches (Settings -> Security).

Two config keys decide whether the agent can skip or change the owner's tool
approvals, and both are read-only on the Advanced settings page
(``config_all_settings.READONLY_REASONS``, ``WHY_SAFETY``) so that a generic
config write cannot flip them:

* ``mcp.protect_kiro_permission_rules`` -- refuse the agent's file-edit writes to
  kiro-cli's permission rule files (``security.paths._permission_rule_rows``).
* ``mcp.honour_auto_approve`` -- keep an ``autoApprove`` list on an MCP server, so
  its verbs run with no approval card (``platform.governance._auto_approve_is_honoured``).

These routes are the one dashboard writer for them, in the shape of the
credential-redaction switch (``credential_redaction.py``): OWNER-gated on both
verbs, not merely token-authenticated, and every change is SEL-audited
synchronously BEFORE it is written. A change whose audit record cannot be
written does not happen. The write goes through the same locked config writer
the Settings PATCH uses (``update_config_locked`` under the dashboard config
lock), never a raw file write.

Known limit, the same one the redaction switch states: the owner gate is an
identity check, not proof that a human clicked. Every change leaves an SEL record.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

logger = logging.getLogger(__name__)

#: The only config paths these routes read or write.
SWITCH_KEYS: tuple[str, ...] = (
    "mcp.protect_kiro_permission_rules",
    "mcp.honour_auto_approve",
)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INVALID_JSON = "invalid_json"
_CODE_INVALID_BODY = "invalid_body"
_CODE_UNKNOWN_KEY = "unknown_switch"
_CODE_LOCKED = "locked_by_policy"
_CODE_OVERLAY = "config_overlay_owned"
_CODE_AUDIT_UNAVAILABLE = "safety_switch_audit_unavailable"
_CODE_WRITE_FAILED = "safety_switch_write_failed"

_SOURCE = "safety-switches"


def _audit(
    *,
    outcome: str,
    caller: str,
    operation: str,
    detail: str = "",
    critical: bool = False,
) -> bool:
    """Record a read, a change or a refusal in the SEL; return whether it was written.

    Never raises. ``critical=True`` makes the write synchronous and fail-loud: the
    default ``sel.log`` only enqueues, so its ``True`` proves nothing. Change
    records pass it (they run on a worker thread); reads and refusals do not.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=f"safety_switches.{operation}",
            outcome=outcome,
            source=_SOURCE,
            resources=detail[:200],
            critical=critical,
        )
        return True
    except Exception:
        logger.debug("could not write the safety-switch audit event", exc_info=True)
        return False


class _AuditUnavailable(Exception):
    """The change was refused because its audit record could not be written."""


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER; audited off the event loop."""
    if is_owner_dashboard_request(request):
        return None
    logger.warning(
        "refused %s: the safety switches are a dashboard owner action (app=%s)",
        operation,
        request.get("app"),
    )
    await asyncio.to_thread(
        _audit,
        outcome="denied",
        caller=str(request.get("user") or "anonymous"),
        operation=operation,
        detail=f"non-owner caller refused (app={request.get('app')!r})",
    )
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


def _read_state() -> dict[str, Any]:
    """Both switches as configured, and as the gate that reads each one sees it now.

    ``in_force`` comes from the same reader the enforcing code uses, so a degraded
    config (the fence then stays on) shows here as it acts. ``degraded`` says the
    config could not be read in full.
    """
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.governance import _auto_approve_is_honoured
    from kiro_crew.security.paths import _permission_rule_rows

    cfg = live.snapshot() or KiroCrewConfig.load()
    degraded = bool(
        cfg._base_unreadable or cfg._overlay_unreadable or "mcp" in cfg.degraded_sections
    )
    return {
        "degraded": degraded,
        "switches": [
            {
                "key": "mcp.protect_kiro_permission_rules",
                "enabled": bool(cfg.mcp.protect_kiro_permission_rules),
                "in_force": bool(_permission_rule_rows()),
            },
            {
                "key": "mcp.honour_auto_approve",
                "enabled": bool(cfg.mcp.honour_auto_approve),
                "in_force": _auto_approve_is_honoured(),
            },
        ],
    }


async def api_safety_switches_get(request: web.Request) -> web.Response:
    """GET /api/security/safety-switches -- both switches, owner only."""
    denied = await _deny_non_owner(request, "read")
    if denied:
        return denied
    state = await asyncio.to_thread(_read_state)
    await asyncio.to_thread(
        _audit,
        outcome="allowed",
        caller=str(request.get("user") or "owner"),
        operation="read",
        detail=",".join(f"{s['key']}={s['enabled']}" for s in state["switches"]),
    )
    return web.json_response(state)


async def api_safety_switches_put(request: web.Request) -> web.Response:
    """PUT /api/security/safety-switches -- record ``{"key": <path>, "enabled": bool}``.

    ``key`` must be one of :data:`SWITCH_KEYS` and ``enabled`` a JSON boolean;
    anything else is a 400 and nothing is written. A key this build locks, or one
    ``config.local.json`` sets, is a 409, as the Settings PATCH answers it.
    """
    from kiro_crew.config import loosening_provenance
    from kiro_crew.config.loader import (
        ConfigReadError,
        ConfigWriteRefused,
        config_path,
        overlay_pins,
        update_config_locked,
    )
    from kiro_crew.dashboard.handlers.agents import _get_config_lock
    from kiro_crew.dashboard.handlers.core import _hot_apply_after_write
    from kiro_crew.edition_locks import LOCKED_BY_POLICY, config_path_locked

    denied = await _deny_non_owner(request, "write")
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    key, enabled = body.get("key"), body.get("enabled")
    if key not in SWITCH_KEYS:
        return web.json_response(
            {"error": "key is not a safety switch", "code": _CODE_UNKNOWN_KEY}, status=400
        )
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": _CODE_INVALID_BODY}, status=400
        )
    subject = str(request.get("user") or "owner")
    section, leaf = key.split(".")
    if config_path_locked(key):
        await asyncio.to_thread(
            _audit, outcome="denied", caller=subject, operation="write", detail=f"{key}: locked"
        )
        return web.json_response({"error": LOCKED_BY_POLICY, "code": _CODE_LOCKED}, status=409)
    if await asyncio.to_thread(overlay_pins, section, leaf):
        await asyncio.to_thread(
            _audit, outcome="denied", caller=subject, operation="write", detail=f"{key}: overlay"
        )
        return web.json_response(
            {
                "error": f"{key} is set in config.local.json, which overrides this setting. "
                "Change or remove it there.",
                "code": _CODE_OVERLAY,
            },
            status=409,
        )

    def _mutate(data: dict) -> dict:
        target = data.setdefault(section, {})
        if not isinstance(target, dict):
            raise ValueError(f"config section '{section}' is not an object")
        target[leaf] = enabled
        return data

    def _audit_and_write() -> None:
        # One unit on the worker thread, under the config lock the caller holds:
        # the record first, then the write, so a cancelled handler cannot change
        # a switch without its trace. No record, no change, in either direction.
        # A config.json edit made outside Kiro Crew that waits for review refuses
        # the save; check it first, so a refused save leaves no change record.
        loosening_provenance.refuse_save_if_pending(loosening_provenance.LEAF_CONFIG)
        if not _audit(
            outcome="enabled" if enabled else "disabled",
            caller=subject,
            operation="write",
            detail=key,
            critical=True,
        ):
            raise _AuditUnavailable()
        update_config_locked(config_path(), mutate=_mutate)

    async with _get_config_lock():
        try:
            await asyncio.to_thread(_audit_and_write)
        except _AuditUnavailable:
            logger.error("refused to change %s: the security event log is unavailable", key)
            return web.json_response(
                {
                    "error": "the security event log is unavailable, so the switch is unchanged",
                    "code": _CODE_AUDIT_UNAVAILABLE,
                },
                status=503,
            )
        except ConfigWriteRefused as exc:
            # A refusal of the owner's own save (a pending outside edit, the publish
            # floor), answered as the Settings PATCH answers it: its code and message.
            await asyncio.to_thread(
                _audit,
                outcome="denied",
                caller=subject,
                operation="write",
                detail=f"{key}: {exc.code}",
            )
            return web.json_response({"error": str(exc), "code": exc.code}, status=409)
        except (ConfigReadError, OSError, ValueError) as exc:
            logger.error("could not write %s: %s", key, type(exc).__name__)
            await asyncio.to_thread(
                _audit,
                outcome="write_failed",
                caller=subject,
                operation="write",
                detail=f"{key}: {type(exc).__name__}",
            )
            return web.json_response(
                {"error": "could not save the switch", "code": _CODE_WRITE_FAILED}, status=500
            )
    # Same follow-up as the Settings PATCH: the live config reloads before the
    # reply, so the state returned below is the one the gates read now.
    await _hot_apply_after_write()
    return web.json_response(await asyncio.to_thread(_read_state))

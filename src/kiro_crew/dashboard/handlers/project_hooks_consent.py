"""Dashboard handler for the owner's per-folder project-hooks choice.

``PUT /api/project-hooks/consent`` ``{"folder": str, "enabled": bool}`` records
whether a KAS session in *folder* runs a folder agent's own ``hooks``
(``kiro_crew.project_hooks_consent``). ``GET /api/project-hooks/consent?folder=``
reads one folder's current state back, read-only. Both are OWNER-gated, the same
gate the other consent records use: a Slack-allow-listed non-owner authenticates
with ``app == ""`` too, and the owner predicate refuses them. Every change is
SEL-audited, and so is a refused read.

Known limit, as for the other owner switches: the owner gate is an identity check,
not proof that a human clicked.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TypeGuard

from aiohttp import web

from kiro_crew import project_hooks_consent
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INVALID_JSON = "invalid_json"
_CODE_INVALID_BODY = "project_hooks_consent_invalid_body"
_CODE_WRITE_FAILED = "project_hooks_consent_write_failed"
_CODE_STORE_FULL = "project_hooks_consent_store_full"

#: The longest folder path accepted, so a request cannot grow the record unbounded.
_MAX_FOLDER_CHARS = 4096


def _audit(*, caller: str, outcome: str, detail: str = "") -> None:
    """Record a change or a refusal in the SEL. Never raises."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=f"project_hooks_consent.{outcome}",
            outcome=outcome,
            source="project-hooks-consent",
            resources=detail[:200],
        )
    except Exception:  # noqa: BLE001 - an audit failure must not break the request
        logger.debug("could not write the project-hooks consent audit event", exc_info=True)


def _folder_param_ok(folder: object) -> TypeGuard[str]:
    """The PUT's own folder check: a non-empty absolute path within the length bound."""
    return (
        isinstance(folder, str)
        and bool(folder)
        and len(folder) <= _MAX_FOLDER_CHARS
        and os.path.isabs(folder)
    )


def _folder_state(folder: str) -> dict | None:
    """``{"state", "enabled"}`` for one folder, or ``None`` when it is not a directory.

    ``state`` is the owner's Crew choice (``on``, ``off``, or ``unset``; a record
    that cannot be read is ``off``) and ``enabled`` whether the hooks run now
    (kiro-cli's trust included). Only these two values: no other folder, path or
    record content. Read-only, and any failure reads as off.
    """
    if project_hooks_consent.folder_key(folder) is None:
        return None
    try:
        choice = project_hooks_consent.crew_choice(folder)
        enabled, _source = project_hooks_consent.hooks_enabled(folder)
    except Exception:  # noqa: BLE001 - fail closed: an unread state is off
        logger.debug("could not read the project-hooks choice", exc_info=True)
        return {"state": "off", "enabled": False}
    if choice is None:
        state = "unset"
    elif choice == project_hooks_consent.ON:
        state = "on"
    else:  # OFF, or UNREADABLE
        state = "off"
    return {"state": state, "enabled": bool(enabled)}


async def api_project_hooks_consent_get(request: web.Request) -> web.Response:
    """GET /api/project-hooks/consent?folder= -- one folder's current choice, read-only.

    Same owner gate and folder check as the PUT, so the notice card can show the
    real state after a reload instead of the state its old transcript row recorded.
    """
    if not is_owner_dashboard_request(request):
        caller = str(request.get("user") or "anonymous")
        await asyncio.to_thread(_audit, caller=caller, outcome="denied", detail="non-owner read")
        return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)
    folder = request.query.get("folder")
    if not _folder_param_ok(folder):
        return web.json_response(
            {"error": "folder must be an absolute path", "code": _CODE_INVALID_BODY}, status=400
        )
    found = await asyncio.to_thread(_folder_state, folder)
    if found is None:
        return web.json_response(
            {"error": "folder is not an existing directory", "code": _CODE_INVALID_BODY}, status=400
        )
    # Per-folder and owner-only: never stored by a cache another viewer could read.
    return web.json_response(found, headers={"Cache-Control": "no-store"})


async def api_project_hooks_consent_put(request: web.Request) -> web.Response:
    """PUT /api/project-hooks/consent -- record ``on`` or ``off`` for one folder."""
    caller = str(request.get("user") or "anonymous")
    if not is_owner_dashboard_request(request):
        await asyncio.to_thread(_audit, caller=caller, outcome="denied", detail="non-owner caller")
        return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    folder = body.get("folder") if isinstance(body, dict) else None
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool) or not _folder_param_ok(folder):
        return web.json_response(
            {
                "error": "folder must be an absolute path and enabled a boolean",
                "code": _CODE_INVALID_BODY,
            },
            status=400,
        )
    try:
        key = await asyncio.to_thread(project_hooks_consent.set_choice, folder, enabled)
    except ValueError:
        return web.json_response(
            {"error": "folder is not an existing directory", "code": _CODE_INVALID_BODY}, status=400
        )
    except project_hooks_consent.StoreFull as exc:
        return web.json_response({"error": str(exc), "code": _CODE_STORE_FULL}, status=409)
    except OSError as exc:
        logger.error("could not record the project-hooks choice: %s", type(exc).__name__)
        return web.json_response(
            {"error": "could not record the choice", "code": _CODE_WRITE_FAILED}, status=500
        )
    await asyncio.to_thread(
        _audit, caller=caller, outcome="enabled" if enabled else "disabled", detail=key
    )
    return web.json_response({"ok": True, "enabled": enabled})

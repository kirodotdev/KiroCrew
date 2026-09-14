"""Separate owner and authenticated-member organization HTTP surfaces."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_utils import _redact_value
from kiro_crew.dashboard.handlers._shared import (
    internal_memory_scope,
    require_owner_dashboard_request,
)
from kiro_crew.organization import OWNER, OrganizationError, OrganizationStore
from kiro_crew.organization_service import (
    apply_action,
    owner_chat_request,
    runtime_status,
    verify_member,
)
from kiro_crew.organization_tools import ORGANIZATION_SCHEMAS, validate
from kiro_crew.validation import ValidationError

logger = logging.getLogger(__name__)


def _refusal(exc: OrganizationError) -> web.Response:
    return web.json_response(
        {"error": _redact_value(str(exc)), "code": exc.code}, status=exc.status
    )


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (ValueError, UnicodeError) as exc:
        raise OrganizationError("invalid_json", "Send a JSON object.", 400) from exc
    if not isinstance(value, dict):
        raise OrganizationError("invalid_json", "Send a JSON object.", 400)
    return value


async def _respond(request: web.Request, store: OrganizationStore, actor: str) -> web.Response:
    try:
        if actor == OWNER:
            from kiro_crew.organization_runtime import ensure_runner

            await ensure_runner(request.app)
        if request.method == "GET":
            result = await asyncio.to_thread(store.snapshot, actor)
            if actor == OWNER:
                result["runtime"] = await asyncio.to_thread(runtime_status)
            return web.json_response(_redact_value(result))
        values = await _body(request)
        action = values.pop("action", "")
        if not isinstance(action, str):
            raise OrganizationError("invalid_action", "Name an organization action.", 400)
        if actor != OWNER:
            name = f"org_{action}"
            if name not in ORGANIZATION_SCHEMAS or name == "org_inbox":
                raise OrganizationError(
                    "action_denied", "This action is not available to members.", 403
                )
            values = validate(name, values)
        elif any(
            field in values for field in ("actor", "session_key", "memory_store", "reservation")
        ):
            raise OrganizationError(
                "invalid_field", "Identity and reservations are resolved by the gateway.", 400
            )
        if action == "start_task" and actor != OWNER:
            request_id = owner_chat_request(
                request.app["state"], request.headers.get("X-Session-Key", "")
            )
            task_id = await asyncio.to_thread(
                store.start_task_from_chat, actor, request_id, **values
            )
            result = {"task_id": task_id}
        else:
            result = await apply_action(store, actor, action, values)
        return web.json_response(_redact_value({"ok": True, **result}))
    except OrganizationError as exc:
        return _refusal(exc)
    except (ValidationError, KeyError, TypeError) as exc:
        return _refusal(OrganizationError("invalid_arguments", str(exc), 400))
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("Organization operation failed")
        return _refusal(
            OrganizationError(
                "organization_unavailable",
                "The organization could not be read or updated. Retry after checking its runtime and storage.",
                503,
            )
        )


async def api_organization(request: web.Request) -> web.Response:
    if request.get("internal_auth") is True or request.get("app"):
        return _refusal(
            OrganizationError(
                "owner_only", "Use the owner's dashboard to manage the organization.", 403
            )
        )
    denied = await require_owner_dashboard_request(request, "organization.manage")
    if denied is not None:
        return denied
    return await _respond(request, OrganizationStore(), OWNER)


async def api_organization_agent(request: web.Request) -> web.Response:
    if request.get("internal_auth") is not True or request.get("app"):
        return _refusal(
            OrganizationError(
                "member_session_required",
                "Organization tools require a verified private member session.",
                403,
            )
        )
    scope, denied = await internal_memory_scope(
        request, "organization.member", claimed_session=request.headers.get("X-Session-Key", "")
    )
    if denied is not None:
        return denied
    if not scope:
        return _refusal(
            OrganizationError(
                "member_session_required", "This caller has no private member identity.", 403
            )
        )
    store = OrganizationStore()
    try:
        member = await asyncio.to_thread(store.member_for_store, scope)
        if member is None or member["state"] != "active":
            raise OrganizationError(
                "organization_member_required",
                "This private member is not active in an organization.",
                403,
            )
        await asyncio.to_thread(verify_member, member)
        return await _respond(request, store, member["id"])
    except OrganizationError as exc:
        return _refusal(exc)
    except (ValueError, OSError, sqlite3.Error):
        logger.exception("Organization member identity unavailable")
        return _refusal(
            OrganizationError(
                "member_identity_unavailable",
                "The member's current identity could not be verified.",
                403,
            )
        )

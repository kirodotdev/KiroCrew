"""Settings → Messaging Channels → Slack → Share Slack access: the switch and its list.

``slack.shared_access`` lets the Slack users the owner lists drive this Crew as
the owner (:mod:`kiro_crew.slack.shared_access`). These routes are how the owner
turns it on, off, and edits the list:

* ``GET /api/slack/shared-access`` -- the switch and each listed id with the name
  Slack gives it and whether Slack confirmed it.
* ``PUT /api/slack/shared-access`` -- ``{"enabled": bool}``. Turning it on also
  needs ``"confirm": true``, which the page sends only after its warning dialog.
* ``POST /api/slack/shared-access/users`` -- ``{"user_id": "U..."}``. Refused
  unless Slack confirms the id now (same workspace as the bot, a person, not
  deactivated, not the owner).
* ``DELETE /api/slack/shared-access/users/{user_id}``.

Every route is the dashboard owner's (``require_owner_dashboard_request``). Writes
are accepted only from this machine's own browser (like the rest of the Slack
settings) and never from an app token or an internal caller. A write answers once
the gateway's live config holds it, or 409 ``not_applied`` when the watcher
deferred it. Each write is audited with the id it names; no message text exists
here to log.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew.config.sections_slack import is_slack_user_id, shared_user_ids
from kiro_crew.dashboard.origin import is_direct_local_request
from kiro_crew.sel import sel
from kiro_crew.slack import shared_access

#: Parallel Slack lookups on a GET; the list itself has no limit.
_LOOKUP_CONCURRENCY = 4


def _deny(caller: str, operation: str, msg: str, *, code: str, status: int = 400) -> web.Response:
    sel().log_api_access(
        caller=caller, operation=operation, outcome="denied", source="dashboard", error=msg
    )
    return web.json_response({"error": msg, "code": code}, status=status)


async def _owner_refusal(request: web.Request, operation: str) -> web.Response | None:
    """The shared owner gate: ``None`` for the dashboard owner, else its refusal."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


def _write_refusal(request: web.Request) -> str:
    """Why this caller may not change shared access, or ``""``."""
    if request.get("internal_auth") is True or request.get("app"):
        return "owner only"
    if not is_direct_local_request(request):
        return "read-only from remote sessions (local machine only)"
    return ""


async def _slack_identity() -> tuple[str, str]:
    """``(bot token, owner id)`` from the stored credentials, read off the loop."""
    from kiro_crew.config.loader import CRED_OWNER_ID, CRED_SLACK_BOT_TOKEN, KiroCrewConfig

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    creds = await asyncio.to_thread(cfg.load_credentials, propagate=False)
    return creds.get(CRED_SLACK_BOT_TOKEN, ""), creds.get(CRED_OWNER_ID, "")


def _web_client(token: str) -> Any:
    from slack_sdk.web.async_client import AsyncWebClient

    return AsyncWebClient(token=token, timeout=10) if token else None


def _user_view(verdict: shared_access.Verdict) -> dict[str, Any]:
    return {
        "user_id": verdict.user_id,
        "name": verdict.name,
        "confirmed": verdict.ok,
        "reason": verdict.reason,
    }


async def api_shared_access_get(request: web.Request) -> web.Response:
    """GET /api/slack/shared-access.

    Reads the live config Slack admits on, not the saved file: after a write the
    gateway did not apply (409 ``not_applied``), the page shows who still has access.
    """
    gate_refusal = await _owner_refusal(request, "slack.shared_access.read")
    if gate_refusal is not None:
        return gate_refusal
    shared = _live_shared()
    token, owner_id = await _slack_identity()
    client = _web_client(token)
    gate = asyncio.Semaphore(_LOOKUP_CONCURRENCY)

    async def _one(user_id: str) -> dict[str, Any]:
        async with gate:
            return _user_view(await shared_access.lookup(client, user_id, owner_id=owner_id))

    users = await asyncio.gather(*(_one(uid) for uid in shared.user_ids))
    return web.json_response(
        {
            "enabled": shared.enabled,
            "users": list(users),
            "read_only": bool(_write_refusal(request)),
        }
    )


async def _commit(mutate_shared: Any) -> dict[str, Any]:
    """Apply *mutate_shared* to ``slack.shared_access`` as re-read under both locks.

    Returns the stored section. The live config is updated before this returns, so
    the next Slack event already sees the change.
    """
    from kiro_crew.config import loader
    from kiro_crew.config.loader_document import update_config_locked
    from kiro_crew.dashboard.chat_utils_awaits import run_config_write
    from kiro_crew.dashboard.handlers.core import _hot_apply_after_write

    result: dict[str, Any] = {}

    def _mutate(doc: dict) -> dict:
        raw_slack = doc.get("slack")
        slack: dict[str, Any] = raw_slack if isinstance(raw_slack, dict) else {}
        raw_current = slack.get("shared_access")
        current: dict[str, Any] = raw_current if isinstance(raw_current, dict) else {}
        section = {
            "enabled": current.get("enabled") is True,
            "user_ids": shared_user_ids(current.get("user_ids")),
        }
        mutate_shared(section)
        slack["shared_access"] = section
        doc["slack"] = slack
        result.update(section)
        return doc

    def _write() -> None:
        update_config_locked(loader.config_path(), mutate=_mutate)

    await run_config_write(_write)
    await _hot_apply_after_write()
    return result


def _live_shared() -> Any:
    """``slack.shared_access`` as the gateway's live config holds it (what Slack admits on)."""
    from kiro_crew.slack import handler

    return handler.slack_cfg().slack.shared_access


def _live_matches(stored: dict[str, Any]) -> bool:
    """Whether the gateway's live config now holds *stored*.

    False when the watcher deferred the write (another ``slack.*`` field is
    malformed), so a removal the caller is told about has not taken effect.
    """
    live = _live_shared()
    return live.enabled == stored["enabled"] and list(live.user_ids) == stored["user_ids"]


def _not_applied(caller: str, operation: str) -> web.Response:
    return _deny(
        caller,
        operation,
        "saved, but the gateway has not applied it: fix the other Slack settings in config.json",
        status=409,
        code="not_applied",
    )


async def api_shared_access_put(request: web.Request) -> web.Response:
    """PUT /api/slack/shared-access -- turn shared access on or off."""
    caller = request.get("user", "dashboard")
    op = "slack.shared_access.switch"
    gate_refusal = await _owner_refusal(request, op)
    if gate_refusal is not None:
        return gate_refusal
    refusal = _write_refusal(request)
    if refusal:
        return _deny(caller, op, refusal, code="write_refused", status=403)
    try:
        body = await request.json()
    except Exception:
        return _deny(caller, op, "invalid JSON", code="invalid_json")
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return _deny(caller, op, "enabled must be a boolean", code="invalid_enabled")
    enabled = body["enabled"]
    if enabled and body.get("confirm") is not True:
        return _deny(caller, op, "turning shared access on needs confirm", code="confirm_required")

    def _set(section: dict) -> None:
        section["enabled"] = enabled

    stored = await _commit(_set)
    if not _live_matches(stored):
        return _not_applied(caller, op)
    sel().log_api_access(
        caller=caller,
        operation=op,
        outcome="ok",
        source="dashboard",
        resources="enabled" if enabled else "disabled",
    )
    return web.json_response({"ok": True, "enabled": stored["enabled"]})


async def api_shared_access_add(request: web.Request) -> web.Response:
    """POST /api/slack/shared-access/users -- add one Slack user, once Slack confirms it."""
    caller = request.get("user", "dashboard")
    op = "slack.shared_access.add"
    gate_refusal = await _owner_refusal(request, op)
    if gate_refusal is not None:
        return gate_refusal
    refusal = _write_refusal(request)
    if refusal:
        return _deny(caller, op, refusal, code="write_refused", status=403)
    try:
        body = await request.json()
    except Exception:
        return _deny(caller, op, "invalid JSON", code="invalid_json")
    raw = body.get("user_id") if isinstance(body, dict) else None
    user_id = raw.strip() if isinstance(raw, str) else ""
    if not is_slack_user_id(user_id):
        return _deny(
            caller,
            op,
            "user_id must be a Slack member ID (starts with U or W)",
            code=shared_access.REASON_MALFORMED,
        )
    token, owner_id = await _slack_identity()
    verdict = await shared_access.lookup(
        _web_client(token), user_id, owner_id=owner_id, refresh=True
    )
    if not verdict.ok:
        return _deny(caller, op, f"Slack did not confirm {user_id}", code=verdict.reason)

    def _add(section: dict) -> None:
        if user_id not in section["user_ids"]:
            section["user_ids"].append(user_id)

    if not _live_matches(await _commit(_add)):
        return _not_applied(caller, op)
    sel().log_api_access(
        caller=caller, operation=op, outcome="ok", source="dashboard", resources=user_id
    )
    return web.json_response({"ok": True, "user": _user_view(verdict)})


async def api_shared_access_remove(request: web.Request) -> web.Response:
    """DELETE /api/slack/shared-access/users/{user_id} -- applies to the next event."""
    caller = request.get("user", "dashboard")
    op = "slack.shared_access.remove"
    gate_refusal = await _owner_refusal(request, op)
    if gate_refusal is not None:
        return gate_refusal
    refusal = _write_refusal(request)
    if refusal:
        return _deny(caller, op, refusal, code="write_refused", status=403)
    user_id = request.match_info.get("user_id", "")
    if not is_slack_user_id(user_id):
        return _deny(caller, op, "invalid user ID", code=shared_access.REASON_MALFORMED)

    def _remove(section: dict) -> None:
        section["user_ids"] = [u for u in section["user_ids"] if u != user_id]

    stored = await _commit(_remove)
    # A re-listed id is checked with Slack again, not admitted from the cache.
    shared_access.forget(user_id)
    if not _live_matches(stored):
        return _not_applied(caller, op)
    sel().log_api_access(
        caller=caller, operation=op, outcome="ok", source="dashboard", resources=user_id
    )
    return web.json_response({"ok": True})


def register(app: web.Application) -> None:
    """Register the shared-access routes (called from the messaging route slice)."""
    app.router.add_get("/api/slack/shared-access", api_shared_access_get)
    app.router.add_put("/api/slack/shared-access", api_shared_access_put)
    app.router.add_post("/api/slack/shared-access/users", api_shared_access_add)
    app.router.add_delete("/api/slack/shared-access/users/{user_id}", api_shared_access_remove)

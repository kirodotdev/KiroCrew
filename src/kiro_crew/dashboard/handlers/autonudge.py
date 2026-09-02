"""Auto-nudge HTTP API — list / start / stop / update loops for chat slots."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.autonudge import get_instance as _autonudge_get
from kiro_crew.autonudge import is_structured_monitor_loop, structured_monitor_binding_key_for

# The security chokepoint lives in the transport-agnostic module (see its
# docstring); re-exported here so existing importers keep working. This file
# is intentionally a THIN HTTP mapping over it.
from kiro_crew.autonudge_authz import (  # noqa: F401 - re-exported
    authorize_and_add_nudge,
    authorize_and_stop_monitor,
    authorize_and_update_monitor,
    authorize_and_update_nudge,
    resolve_stop_sentinel,
)
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
    stale_owner_session_response,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_AGENT_TURNS,
    DEFAULT_MONITOR_CADENCE_SECS,
    DEFAULT_MONITOR_PROVIDER_ERRORS,
    DEFAULT_MONITOR_RUNTIME_SECS,
    DEFAULT_MONITOR_TOKENS,
    MAX_MONITOR_AGENT_TURNS,
    MAX_MONITOR_CADENCE_SECS,
    MAX_MONITOR_PROVIDER_ERRORS,
    MAX_MONITOR_RUNTIME_SECS,
    MAX_MONITOR_TOKENS,
    MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
    MIN_MONITOR_CADENCE_SECS,
    MONITOR_STATE_VERSION,
    MONITOR_STOP_UNSUPPORTED_VERSION,
    MonitorBudgets,
    MonitorState,
    monitor_state_public_dict,
)
from kiro_crew.platform import redact_via_context
from kiro_crew.sel import sel
from kiro_crew.session_ledger import ledger_key, render_snapshot

logger = logging.getLogger(__name__)

_CODE_DASHBOARD_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INTERNAL_SECRET_REQUIRED = "internal_secret_required"


def render_nudge_message(message: str, stop_sentinel_path: str | None) -> str:
    """Replace {{STOP_FILE}} template with the resolved sentinel path."""
    return message.replace("{{STOP_FILE}}", stop_sentinel_path or "")


async def compose_nudge_body(
    message: str, stop_sentinel_path: str | None, slot_key: str | None
) -> str:
    """Compose one nudge cycle's full body text — the shared fire-path composer.

    Applies :func:`render_nudge_message`'s template substitution and, when the
    loop's session has a non-empty, non-terminal work ledger, prefixes a
    compact snapshot of it so every cycle starts from the durable state
    instead of from transcript memory. Derived server-side at fire time;
    sessions without a ledger render exactly as before.

    The ledger read is filesystem I/O, so it runs in a worker thread — a slow
    or wedged filesystem costs this loop's snapshot, never the event loop.
    Best-effort throughout: a snapshot failure must not cost the nudge itself.
    """
    body = render_nudge_message(message, stop_sentinel_path)
    if slot_key:
        try:
            snapshot = await asyncio.to_thread(render_snapshot, ledger_key(slot_key))
        except Exception:
            logger.debug("nudge: ledger snapshot failed for %s", slot_key, exc_info=True)
            snapshot = ""
        if snapshot:
            return f"{snapshot}\n\n{body}"
    return body


def _redact_monitor_value(value: Any) -> Any:
    """Redact every string in provider-controlled monitor evidence."""
    if isinstance(value, str):
        return redact_via_context(value)
    if isinstance(value, dict):
        return {
            _redact_monitor_value(key): _redact_monitor_value(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_monitor_value(item) for item in value]
    return value


def _serialize(loop: Any) -> dict[str, Any]:
    payload = asdict(loop)
    if loop.monitor is None:
        # Legacy clients predate structured monitors and require their exact shape.
        payload.pop("monitor", None)
    else:
        payload["monitor"] = _redact_monitor_value(monitor_state_public_dict(loop.monitor))
    return payload


def _serialize_monitor(loop: Any) -> dict[str, Any]:
    return _serialize(loop)


def _monitor_error(message: str, code: str, *, status: int = 400) -> web.Response:
    response = web.json_response({"error": message, "code": code})
    response.set_status(status)
    return response


async def _audit_monitor_access(
    request: web.Request,
    operation: str,
    outcome: str,
    *,
    error: str = "",
) -> None:
    """Record a monitor authorization decision off the event-loop thread."""
    try:
        await asyncio.to_thread(
            lambda: sel().log_api_access(
                caller=str(
                    request.get("user")
                    or request.headers.get("X-Session-Key")
                    or request.remote
                    or "unknown"
                ),
                operation=operation,
                outcome=outcome,
                source="dashboard",
                resources=request.path,
                error=error,
            )
        )
    except Exception:
        logger.debug("Could not audit %s monitor access", operation, exc_info=True)


async def _require_monitor_owner(
    request: web.Request,
    operation: str,
) -> web.Response | None:
    """Require the configured dashboard owner before reading monitor state."""
    if is_owner_dashboard_request(request):
        await _audit_monitor_access(request, operation, "allowed")
        return None
    await _audit_monitor_access(
        request,
        operation,
        "denied",
        error="dashboard owner required",
    )
    stale = stale_owner_session_response(request)
    if stale is not None:
        return stale
    return _monitor_error(
        "dashboard owner required",
        _CODE_DASHBOARD_OWNER_REQUIRED,
        status=403,
    )


async def _require_monitor_internal(request: web.Request) -> web.Response | None:
    """Require proven internal-secret authentication for session-key reads."""
    if request.get("internal_auth") is True:
        await _audit_monitor_access(request, "session_monitor_get", "allowed")
        return None
    await _audit_monitor_access(
        request,
        "session_monitor_get",
        "denied",
        error="internal secret required",
    )
    return _monitor_error(
        "internal secret required",
        _CODE_INTERNAL_SECRET_REQUIRED,
        status=403,
    )


def _bounded_int(body: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = body.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return raw


def _monitor_config(body: dict[str, Any]) -> MonitorState:
    from kiro_crew.monitoring.github_pull_request import parse_github_pull_request_target

    kind = body.get("kind", "github_pull_request")
    objective = body.get("objective", "review_ready")
    if kind != "github_pull_request" or objective != "review_ready":
        raise ValueError("only github_pull_request review_ready monitors are supported")
    target = parse_github_pull_request_target(body.get("target", "")).url
    wake = body.get("wake_instructions", "")
    if not isinstance(wake, str) or len(wake) > MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS:
        raise ValueError(
            f"wake_instructions must be a string of at most "
            f"{MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS} characters"
        )
    return MonitorState(
        kind=kind,
        target=target,
        objective=objective,
        created_ts=0.0,
        cadence_secs=_bounded_int(
            body,
            "cadence_secs",
            DEFAULT_MONITOR_CADENCE_SECS,
            MIN_MONITOR_CADENCE_SECS,
            MAX_MONITOR_CADENCE_SECS,
        ),
        budgets=MonitorBudgets(
            max_runtime_secs=_bounded_int(
                body,
                "max_runtime_secs",
                DEFAULT_MONITOR_RUNTIME_SECS,
                1,
                MAX_MONITOR_RUNTIME_SECS,
            ),
            max_agent_turns=_bounded_int(
                body,
                "max_agent_turns",
                DEFAULT_MONITOR_AGENT_TURNS,
                1,
                MAX_MONITOR_AGENT_TURNS,
            ),
            max_tokens=_bounded_int(
                body, "max_tokens", DEFAULT_MONITOR_TOKENS, 1, MAX_MONITOR_TOKENS
            ),
            max_provider_errors=_bounded_int(
                body,
                "max_provider_errors",
                DEFAULT_MONITOR_PROVIDER_ERRORS,
                1,
                MAX_MONITOR_PROVIDER_ERRORS,
            ),
        ),
        wake_instructions=wake.strip(),
    )


async def api_autonudge_list(request: web.Request) -> web.Response:
    """GET /api/autonudge — list all active loops."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "loops": []})
    loops = [_serialize(lp) for lp in svc.list_all() if not is_structured_monitor_loop(lp)]
    return web.json_response({"enabled": True, "loops": loops})


async def api_autonudge_get(request: web.Request) -> web.Response:
    """GET /api/autonudge/{slot_key} — loop bound to this slot (or null)."""
    svc = _autonudge_get()
    slot_key = request.match_info["slot_key"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_slot(slot_key)
    legacy = loop if loop is not None and not is_structured_monitor_loop(loop) else None
    return web.json_response({"enabled": True, "loop": _serialize(legacy) if legacy else None})


async def api_session_monitor_get(request: web.Request) -> web.Response:
    """Return only the structured monitor owned by the authenticated session."""
    denied = await _require_monitor_internal(request)
    if denied is not None:
        return denied
    session_key = request.headers.get("X-Session-Key", "")
    binding = structured_monitor_binding_key_for(session_key)
    if not binding:
        await _audit_monitor_access(
            request,
            "session_monitor_get",
            "denied",
            error="authenticated session binding required",
        )
        return web.json_response(
            {"error": "authenticated session binding required", "code": "session_required"},
            status=401,
        )
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "monitor": None})
    loop = svc.get_by_slot(binding)
    if loop is None or not is_structured_monitor_loop(loop):
        return web.json_response({"enabled": True, "monitor": None})
    monitor = loop.monitor
    assert monitor is not None
    return web.json_response(
        {
            "enabled": True,
            "active": bool(loop.active),
            "monitor_id": loop.id,
            "monitor": _redact_monitor_value(monitor_state_public_dict(monitor)),
        }
    )


async def api_monitors_list(request: web.Request) -> web.Response:
    """GET /api/monitors — structured records, including terminal outcomes."""
    denied = await _require_monitor_owner(request, "monitors_list")
    if denied is not None:
        return denied
    svc = _autonudge_get()
    monitors = (
        []
        if svc is None
        else [_serialize_monitor(lp) for lp in svc.list_all() if is_structured_monitor_loop(lp)]
    )
    return web.json_response({"enabled": svc is not None, "monitors": monitors})


async def api_monitor_slot_get(request: web.Request) -> web.Response:
    """GET /api/monitors/slot/{slot_key} — one dashboard-owned record."""
    denied = await _require_monitor_owner(request, "monitor_slot_get")
    if denied is not None:
        return denied
    svc = _autonudge_get()
    loop = svc.get_by_slot(request.match_info["slot_key"]) if svc is not None else None
    return web.json_response(
        {
            "enabled": svc is not None,
            "monitor": (
                _serialize_monitor(loop)
                if loop is not None and is_structured_monitor_loop(loop)
                else None
            ),
        }
    )


async def api_monitor_create(request: web.Request) -> web.Response:
    """POST /api/monitors — create one bounded structured monitor."""
    denied = await _require_monitor_owner(request, "monitor_create")
    if denied is not None:
        return denied
    svc = _autonudge_get()
    if svc is None:
        return _monitor_error("monitoring disabled", "monitoring_disabled", status=503)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        config = _monitor_config(body)
    except Exception as exc:
        return _monitor_error(str(exc), "invalid_monitor")
    slot_key = str(body.get("slot_key") or "")
    if slot_key.startswith("webex:"):
        return _monitor_error(
            "structured monitoring is not supported for Webex sessions",
            "monitor_session_unsupported",
        )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=request.app["state"],
        slot_key=slot_key,
        message=config.wake_instructions or "structured monitor",
        idle_secs=config.cadence_secs,
        max_cycles=0,
        max_runtime_secs=config.budgets.max_runtime_secs,
        source="dashboard",
        caller=request.remote or "",
        monitor=config,
        replace_existing=False,
    )
    if error is not None:
        return _monitor_error(error, "monitor_create_denied", status=status)
    return web.json_response({"ok": True, "monitor": _serialize_monitor(loop)})


async def api_monitor_update(request: web.Request) -> web.Response:
    """PATCH /api/monitors/{id} — patch a nonterminal structured record."""
    denied = await _require_monitor_owner(request, "monitor_update")
    if denied is not None:
        return denied
    svc = _autonudge_get()
    loop = svc.get_by_id(request.match_info["monitor_id"]) if svc is not None else None
    if loop is None or not is_structured_monitor_loop(loop):
        return _monitor_error("structured monitor not found", "monitor_not_found", status=404)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        current = loop.monitor
        assert current is not None
        merged = {
            "kind": current.kind,
            "target": body.get("target", current.target),
            "objective": body.get("objective", current.objective),
            "cadence_secs": body.get("cadence_secs", current.cadence_secs),
            "max_runtime_secs": body.get("max_runtime_secs", current.budgets.max_runtime_secs),
            "max_agent_turns": body.get("max_agent_turns", current.budgets.max_agent_turns),
            "max_tokens": body.get("max_tokens", current.budgets.max_tokens),
            "max_provider_errors": body.get(
                "max_provider_errors", current.budgets.max_provider_errors
            ),
            "wake_instructions": body.get("wake_instructions", current.wake_instructions),
        }
        config = _monitor_config(merged)
    except Exception as exc:
        return _monitor_error(str(exc), "invalid_monitor")
    patch: dict[str, Any] = {}
    for name in ("target", "objective", "cadence_secs", "wake_instructions"):
        if name in body:
            patch[name] = getattr(config, name)
    budget_fields = {
        "max_runtime_secs",
        "max_agent_turns",
        "max_tokens",
        "max_provider_errors",
    }
    if budget_fields & set(body):
        patch["budget_patch"] = {
            field: getattr(config.budgets, field) for field in budget_fields if field in body
        }
    if not patch:
        return _monitor_error("no monitor fields to update", "monitor_update_empty")
    updated, error, status = await authorize_and_update_monitor(
        svc=svc,
        state=request.app["state"],
        loop_id=loop.id,
        session_key=loop.slot_key,
        patch=patch,
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return _monitor_error(error, "monitor_update_denied", status=status)
    return web.json_response({"ok": True, "monitor": _serialize_monitor(updated)})


async def api_monitor_stop(request: web.Request) -> web.Response:
    """POST /api/monitors/{id}/stop — retain a durable user-stop outcome."""
    denied = await _require_monitor_owner(request, "monitor_stop")
    if denied is not None:
        return denied
    svc = _autonudge_get()
    if svc is None:
        return _monitor_error("monitoring disabled", "monitoring_disabled", status=503)
    loop = svc.get_by_id(request.match_info["monitor_id"])
    if loop is None or not is_structured_monitor_loop(loop):
        return _monitor_error("structured monitor not found", "monitor_not_found", status=404)
    stopped, error, status = await authorize_and_stop_monitor(
        svc=svc,
        loop_id=loop.id,
        session_key=loop.slot_key,
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return _monitor_error(error, "monitor_stop_denied", status=status)
    return web.json_response({"ok": True, "monitor": _serialize_monitor(stopped)})


async def api_monitor_restart(request: web.Request) -> web.Response:
    """POST /api/monitors/{id}/restart — the sole browser revival route."""
    denied = await _require_monitor_owner(request, "monitor_restart")
    if denied is not None:
        return denied
    svc = _autonudge_get()
    loop = svc.get_by_id(request.match_info["monitor_id"]) if svc is not None else None
    if loop is None or not is_structured_monitor_loop(loop):
        return _monitor_error("structured monitor not found", "monitor_not_found", status=404)
    monitor = loop.monitor
    assert monitor is not None
    if monitor.version != MONITOR_STATE_VERSION:
        return _monitor_error(
            "monitor version is unsupported",
            MONITOR_STOP_UNSUPPORTED_VERSION,
            status=409,
        )
    if monitor.outcome is None:
        return _monitor_error("only terminal monitors can restart", "monitor_not_terminal")
    state: DashboardState = request.app["state"]
    restarted, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=loop.slot_key,
        message=monitor.wake_instructions or "structured monitor",
        idle_secs=monitor.cadence_secs,
        max_cycles=0,
        max_runtime_secs=monitor.budgets.max_runtime_secs,
        source="dashboard",
        caller=request.remote or "",
        monitor=monitor,
        expected_existing_monitor_id=loop.id,
        expected_existing_config_generation=monitor.config_generation,
    )
    if error is not None:
        return _monitor_error(error, "monitor_restart_denied", status=status)
    return web.json_response({"ok": True, "monitor": _serialize_monitor(restarted)})


async def api_autonudge_start(request: web.Request) -> web.Response:
    """POST /api/autonudge — start or replace a loop on a slot.

    Body: { slot_key, message, idle_secs?, max_cycles?, max_runtime_secs?,
            stop_sentinel_path?, gate?, banner? }

    ``gate`` defaults to FALSE here: this route arms whatever the goal popover was
    given, and only ``monitor_start`` has the evidence to gate by default. Pass
    ``gate: true`` to probe-gate a loop armed through this route.

    ``banner`` is the optional short stand-in shown in the transcript row
    instead of ``message``; the model still receives ``message`` in full every
    cycle. Omitting it keeps the row exactly as it has always been.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled (KIROCREW_AUTONUDGE not set)",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # idle_secs/max_cycles/max_runtime_secs come straight from the request
    # body: int() raises ValueError on "abc", TypeError on null/list, and
    # OverflowError on float("inf") (1e309 is legal JSON in aiohttp's parser),
    # any of which would surface as a 500 instead of a 400. Non-integral
    # floats are rejected rather than silently truncated (int(1.5) -> 1 would
    # store a value the caller never asked for). Coerce up front and reject
    # bad input, matching the sibling handlers_instances.api_instances_add
    # guard on the same pattern.
    try:
        for _name in ("idle_secs", "max_cycles", "max_runtime_secs"):
            _val = body.get(_name)
            if isinstance(_val, float) and not _val.is_integer():
                return web.json_response(
                    {"error": f"{_name} must be a whole number", "code": "not_a_whole_number"},
                    status=400,
                )
        idle_secs = int(body.get("idle_secs", 60))
        max_cycles = int(body.get("max_cycles", 0))
        max_runtime_secs = int(body.get("max_runtime_secs", 0))
    except (TypeError, ValueError, OverflowError):
        return web.json_response(
            {"error": "idle_secs, max_cycles and max_runtime_secs must be integers"}, status=400
        )
    # The gating opt-out has to exist HERE too, not only on the MCP tool: this is
    # ABSENT MEANS UNGATED on this route, unlike the monitor_start tool. This is a
    # GENERIC arming route: its only caller is the goal popover, where a person
    # types a recurring instruction whose work is usually NOT a pull request. Such
    # an instruction routinely mentions one anyway ("keep driving PR #42"), and
    # gating on that mention throttles the task to the quiet-streak floor and, when
    # that PR is closed or merged, DEACTIVATES a recurring task that had nothing to
    # do with it. The evidence for gating by default is about monitor_start, whose
    # directive sets `gate: true` itself; extending it here was reach, twice.
    #
    # A non-boolean is still refused rather than coerced: `"false"` is truthy and
    # would silently gate a loop that asked not to be.
    raw_gate = body.get("gate")
    if raw_gate is not None and not isinstance(raw_gate, bool):
        return web.json_response(
            {"error": "gate must be a boolean", "code": "not_a_boolean"}, status=400
        )
    gate = False if raw_gate is None else raw_gate
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=(body.get("session_key") or body.get("slot_key") or ""),
        message=(body.get("message") or ""),
        idle_secs=idle_secs,
        max_cycles=max_cycles,
        stop_sentinel_path=(body.get("stop_sentinel_path") or ""),
        max_runtime_secs=max_runtime_secs,
        # Passed through UNCOERCED: the chokepoint owns the type check, the cap
        # and the channel refusal, so a non-string is a 400 from there rather
        # than a silent str() here that would persist "None" as a banner.
        banner=body.get("banner"),
        source="dashboard",
        caller=request.remote or "",
        gate=gate,
        replace_existing=False,
    )
    if error is not None:
        return web.json_response({"error": error, "code": "autonudge_not_armed"}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_update(request: web.Request) -> web.Response:
    """PATCH /api/autonudge/{loop_id} — update message / idle_secs / active / banner.

    Accepting ``banner`` here is what lets a RUNNING loop be quieted without
    re-registering it: re-arming would reset ``cycle_count`` and the wall-clock
    budget anchor, so a loop discovered to be noisy mid-run could not be fixed
    without discarding its accounting.

    Thin HTTP mapping over ``authorize_and_update_nudge``, which owns the
    message redaction, the integer coercion, and the audit-or-deny policy — see
    its docstring for why those live in the transport-agnostic module and not
    here.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    existing = svc.get_by_id(loop_id)
    if existing is not None and is_structured_monitor_loop(existing):
        return _monitor_error(
            "structured monitors must use the monitor update API",
            "structured_monitor_requires_monitor_api",
            status=409,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    loop, error, status = await authorize_and_update_nudge(
        svc=svc,
        loop_id=loop_id,
        message=body.get("message"),
        idle_secs=body.get("idle_secs"),
        max_cycles=body.get("max_cycles"),
        active=body.get("active"),
        max_runtime_secs=body.get("max_runtime_secs"),
        banner=body.get("banner"),
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_delete(request: web.Request) -> web.Response:
    """DELETE /api/autonudge/{loop_id} — stop and remove a loop."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    # Capture slot_key for audit before removal (loop is gone after remove()).
    # Resolved through the shared ``svc.get_by_id`` -- the same accessor the
    # update-path channel refusal uses -- rather than a second inline id-scan.
    existing = svc.get_by_id(loop_id)
    if existing is not None and is_structured_monitor_loop(existing):
        denied = await _require_monitor_owner(request, "monitor_stop")
        if denied is not None:
            return denied
        _stopped, error, status = await authorize_and_stop_monitor(
            svc=svc,
            loop_id=loop_id,
            session_key=existing.slot_key,
            source="dashboard",
            caller=request.remote or "",
        )
        if error is not None:
            return _monitor_error(error, "monitor_stop_denied", status=status)
        return web.json_response({"ok": True})
    await svc.remove(loop_id)
    sel().log_tool_invocation(
        session_key=existing.slot_key if existing else "",
        source="dashboard",
        tool_name="autonudge_delete",
        outcome="success" if existing else "noop",
        metadata={"loop_id": loop_id, "caller": request.remote or ""},
    )
    return web.json_response({"ok": True})

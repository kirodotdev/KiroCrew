"""Auto-nudge HTTP API — list / start / stop / update loops for chat slots."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, fields
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
from kiro_crew.monitoring.registry import (
    GITHUB_PULL_REQUEST,
    REVIEW_READY,
    kind_supports_objective,
    publicly_armable_kinds,
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


#: Legacy loop fields WITHHELD from a structured monitor's legacy projection.
#:
#: The legacy reads carry no owner gate, so what they may publish is decided by
#: ENTITLEMENT, not by convenience: a caller already reaching them is entitled to
#: know that something is monitoring this session, roughly how often, how far in
#: and in what state. It is not entitled to know WHAT is being watched. Each name
#: below is withheld for one of two reasons, and no withheld field is replaced by
#: a null, an empty string or a plausible default -- an absent field reads as
#: "this surface does not carry that", where a faked one reads as fact.
#:
#: OWNER-SCOPED, because it describes the subject or the evidence:
#:
#: * ``monitor`` -- the whole ``monitor_state_public_dict``: target URL,
#:   objective, kind, budgets, wake instructions, the provider-controlled
#:   observation payload and every fingerprint. Published only by
#:   ``/api/monitors`` and its per-slot sibling, both behind
#:   ``_require_monitor_owner``, and to owners only on the ``autonudge_state``
#:   websocket frame via ``broadcast_ws_owners``.
#: * ``message`` -- for a structured monitor this IS the wake instructions.
#:   Both arming paths set ``message=monitor.wake_instructions or "structured
#:   monitor"``, so returning the legacy field verbatim would publish
#:   owner-scoped text through a field that looks innocuous.
#: * ``banner`` -- the same class of agent-authored display text, and a monitor
#:   has no banner to describe.
#: * ``stop_sentinel_path`` -- a filesystem path, and the structured branch of
#:   ``_timer`` returns before the sentinel is ever tested.
#:
#: UNMAINTAINED on the structured path -- but two of these three have a TRUTHFUL
#: equivalent in the monitor's own state, so they are MAPPED (below) rather than
#: withheld. Withholding them was worse than an imprecise truth: the component
#: defaults an absent ``max_cycles`` to 0, whose label reads "0 = infinity", so
#: the panel asserted that a budget-bounded monitor runs forever. A default that
#: lies is a stronger falsehood than a slightly coarse number.
#:
#: ``stop_sentinel_path`` has no equivalent and stays withheld: the structured
#: branch of ``_timer`` returns before the sentinel is ever tested, and a
#: filesystem path is owner-scoped regardless.
#:
#: A GATED prompt loop is in NEITHER case: it carries probe state but still
#: delivers down the legacy path, so its message and its cycle accounting are
#: real, and ``is_structured_monitor_loop`` already excludes it.
_MONITOR_WITHHELD_LEGACY_FIELDS = frozenset(
    {
        "monitor",
        "message",
        "banner",
        "stop_sentinel_path",
        "max_cycles",
        "cycle_count",
        "last_fire_ts",
    }
)

#: Legacy field -> the monitor attribute path carrying its truthful value.
#:
#: Each is the automation's OWN accounting, not a fact about its subject, so none
#: crosses the entitlement boundary -- and this route already publishes
#: ``max_runtime_secs`` straight from ``budgets``, so a second budget is the same
#: class rather than a new one.
#:
#: * ``max_cycles`` <- ``budgets.max_agent_turns``. A legacy "cycle" IS a
#:   delivered turn, which is exactly what that budget bounds.
#: * ``cycle_count`` <- ``agent_turns``, the numerator of the same reading.
#:   Carrying the bound alone would render "0 of 8" for a monitor that has spent
#:   turns, replacing one falsehood with another.
#: * ``last_fire_ts`` <- ``last_completed_at``, which ``autonudge.py`` writes in
#:   the same step as ``wake_count += 1``, so it is when this automation last
#:   delivered; 0.0 means none has, which renders "never" truthfully. NOT
#:   ``last_probe_at`` -- a probe that woke nothing is not a fire, and mapping it
#:   would invent an event.
_MONITOR_MAPPED_LEGACY_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("max_cycles", ("budgets", "max_agent_turns")),
    ("cycle_count", ("agent_turns",)),
    ("last_fire_ts", ("last_completed_at",)),
)

#: The ONE thing a reduced row does NOT carry that a reader may want: how far in
#: the monitor is. `cycle_count` is withheld above because for a monitor it is
#: false, and nothing replaces it here.
#:
#: A `monitor_presence` object (`probe_count`, `wake_count`, `outcome`) was
#: prepared for exactly that and is deliberately NOT shipped on this route. It
#: would have had no reader: the popover rendering that would display it is
#: sequenced separately, and this route's one unproven claim is precisely whether
#: a consumer handles what it is sent. A field whose arrival AND display can be
#: tested in one change belongs in that change. So the liveness gap is REAL and
#: recorded, not silently filled.
#:
#: Consequence a reader must know: a reduced row carries no positive marker
#: saying "this is a monitor". It is told apart by the ABSENCE of the withheld
#: fields, which is weaker than a marker and is the other half of what the
#: rendering change should add.


def _serialize_for_legacy_reader(loop: Any) -> dict[str, Any]:
    """Project ANY loop, structured monitor included, into the legacy shape.

    A structured monitor belongs on these reads: the goal popover is the only
    place a person can see what is armed on a session, so withholding the record
    leaves it reporting nothing armed while a monitor is probing, and hides that
    from a caller entitled to know it.

    Returning the record verbatim is the opposite and worse fault: these routes
    have no owner gate, and the structured record is owner-scoped everywhere else
    it is published. So a structured monitor is reduced HERE, by what the caller
    is entitled to, and the reduction is enforced by never assembling the
    withheld fields rather than by assembling them and deleting them -- no
    intermediate payload holds the monitor for a later edit to leak.

    A plain or gated loop is untouched and still goes through ``_serialize``.
    """
    if not is_structured_monitor_loop(loop):
        return _serialize(loop)
    # Every field that survives the filter is a scalar, which is what keeps this
    # projection JSON-safe without ``asdict``'s recursive copy -- the only nested
    # field on the dataclass is ``monitor``, and it is withheld. A test pins the
    # surviving key set so a new field cannot silently join or skip this route.
    payload = {
        field.name: getattr(loop, field.name)
        for field in fields(loop)
        if field.name not in _MONITOR_WITHHELD_LEGACY_FIELDS
    }
    monitor = loop.monitor
    for name, path in _MONITOR_MAPPED_LEGACY_FIELDS:
        value: Any = monitor
        for attr in path:
            value = getattr(value, attr)
        payload[name] = value
    return payload


def _autonudge_loop_reading(loop: Any) -> dict[str, Any]:
    """Project a plain auto-nudge loop into a bounded, agent-oriented status.

    The reading answers "is a loop armed on this session, and is it firing" from
    inside the session, which the structured-monitor projection cannot: it
    describes only the structured monitor, so an armed auto-nudge loop and no loop
    at all both read as ``monitor: None`` there.

    Only presence, cadence and progress fields are surfaced. The loop's
    ``message`` is agent-controlled free text and is NOT included — it is not
    needed to verify arming, and leaving it out keeps this read narrow.
    """
    return {
        "id": loop.id,
        "active": bool(loop.active),
        "idle_secs": loop.idle_secs,
        "max_cycles": loop.max_cycles,
        "cycle_count": loop.cycle_count,
        "max_runtime_secs": loop.max_runtime_secs,
        "gate": bool(loop.gate),
        "last_fire_ts": loop.last_fire_ts,
        "created_ts": loop.created_ts,
        "next_due_ts": loop.next_due_ts,
        "stopped_reason": loop.stopped_reason,
        "has_banner": bool(loop.banner),
    }


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
    """Record a monitor authorization decision (best-effort).

    A bare enqueue: the SEL singleton is warmed at gateway startup
    (``sel.warm_sel_singleton``), so no per-site thread hop is needed.
    Guarded because a FAILED warm leaves construction to retry here.
    """
    try:
        sel().log_api_access(
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

    kind = body.get("kind", GITHUB_PULL_REQUEST)
    objective = body.get("objective", REVIEW_READY)
    # Both halves: a caller may only name a PUBLICLY ARMABLE kind, and that kind must
    # itself declare the objective. The flat allowlists upstream cannot express the
    # pairing, so this is where it is checked.
    if kind not in publicly_armable_kinds() or not kind_supports_objective(kind, objective):
        raise ValueError(f"no monitored kind {kind!r} supports objective {objective!r}")
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
    """GET /api/autonudge — list every loop, structured monitors included.

    A structured monitor appears as an armed row, so a reader of this list cannot
    mistake a running monitor for nothing armed. It is REDUCED rather than
    returned verbatim -- this route has no owner gate, so it publishes presence,
    cadence, liveness and state, and never what is being watched. See
    ``_serialize_for_legacy_reader`` for the field-by-field reasoning, and
    ``/api/monitors`` for the owner-gated full record.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "loops": []})
    loops = [_serialize_for_legacy_reader(lp) for lp in svc.list_all()]
    return web.json_response({"enabled": True, "loops": loops})


async def api_autonudge_get(request: web.Request) -> web.Response:
    """GET /api/autonudge/slot/{slot_key} — loop bound to this slot (or null).

    ``null`` means exactly one thing: nothing is armed on this slot. A structured
    monitor bound here is returned, under the same entitlement-scoped reduction as
    the list route above, so "armed but withheld" is not a second reading of the
    same answer.
    """
    svc = _autonudge_get()
    slot_key = request.match_info["slot_key"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_slot(slot_key)
    return web.json_response(
        {
            "enabled": True,
            "loop": _serialize_for_legacy_reader(loop) if loop is not None else None,
        }
    )


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
    if loop is None:
        # Nothing is armed on this session. This is the ONLY case that reads as
        # "not armed", and it is DISTINCT from an armed auto-nudge loop below.
        # Collapsing the two into an identical ``monitor: None`` would leave a
        # caller unable to tell an accepted-and-armed loop from an
        # accepted-and-dropped request.
        return web.json_response({"enabled": True, "monitor": None, "autonudge_loop": None})
    if not is_structured_monitor_loop(loop):
        # A plain auto-nudge loop IS armed. ``monitor`` stays None because a
        # structured monitor genuinely does not exist, but ``autonudge_loop``
        # now carries a truthful presence/cadence/progress reading so the caller
        # can verify arming instead of being told "do not assume" with no
        # instrument. The loop's free-text ``message`` is deliberately omitted:
        # this reading answers "is it armed and firing", not "what does it say".
        return web.json_response(
            {"enabled": True, "monitor": None, "autonudge_loop": _autonudge_loop_reading(loop)}
        )
    monitor = loop.monitor
    assert monitor is not None
    return web.json_response(
        {
            "enabled": True,
            "active": bool(loop.active),
            "monitor_id": loop.id,
            "monitor": _redact_monitor_value(monitor_state_public_dict(monitor)),
            "autonudge_loop": None,
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
    # do with it. Gating by default belongs to monitor_start, whose directive sets
    # `gate: true` itself; it does not extend to this route.
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


async def api_autonudge_fire(request: web.Request) -> web.Response:
    """POST /api/autonudge/{loop_id}/fire — run this loop's next cycle now.

    The manual counterpart of the idle timer, for the case the loop's interval
    cannot serve: the operator already knows the thing being waited on has
    changed, so the remaining gap buys nothing. Spelled ``fire`` rather than the
    cron sibling's ``run`` because ``fire`` is this subsystem's own verb
    (``_on_fire``, ``_run_fire_cycle``, ``last_fire_ts``, the ``fired`` event).

    Thin HTTP mapping, as everywhere else in this file: ``svc.fire_now`` owns the
    schedule semantics and the not-registered / not-active / mid-fire refusals,
    and documents why each is load-bearing. Two refusals belong here instead,
    because they are about the transport's own subject rather than the loop:

    * A **structured monitor** is refused with the same 409 code ``PATCH`` uses.
      Those records are driven by ``_on_monitor_tick`` and owned by the monitor
      API, so a manual cycle here would deliver a legacy prompt for a record that
      never takes that path. The goal popover DOES see one -- the read routes
      above return it -- which is why this refusal has to be stated rather than
      left to the reader being unable to reach it.
    * A **busy session** is refused rather than queued, and that is not a fresh
      product decision — the fire path this route arms already made it, with its
      reason written down at the site: queueing "would stack identical 3KB+
      nudges and blow up the context window" (``_fire_dashboard_nudge``). The
      predicate is the repository's canonical one, ``slot.running or
      slot._in_stage_execution``, read here exactly as the cron-injection
      handler reads it (``handlers/messaging.py``) — ``slot.running`` alone is
      False between the stages of a multi-stage plan, so it would let this land
      a concurrent turn on top of the plan. Note the two consumers of that
      predicate diverge deliberately: the cron path QUEUES, this one REFUSES,
      and the nudge path's stated reason is the one that applies here.

      This check is an AFFORDANCE, not a guarantee: a turn that starts between
      it and the fire is still refused by the fire path, which then re-arms with
      backoff. Its whole job is to turn that silence into a 409 the popover can
      show. A loop bound to a channel key has no dashboard slot, so the check
      is skipped and that transport's own busy guard answers.

    Authentication is the same as its ``POST`` / ``PATCH`` / ``DELETE`` siblings
    on this path, deliberately: no new trust boundary, and this route is
    strictly LESS powerful than the ``POST`` beside it, which arms a loop that
    can spend turns until a bound stops it.

    **Every outcome is audited, and the FIRE is audit-or-deny.** The split is the
    one this repository already draws, not a new policy:

    * The **fire** is gated on a ``critical=True`` write that lands BEFORE
      ``fire_now`` arms anything. The default path only ENQUEUES, and on the
      event loop an enqueue failure drops the event with a warning
      (``sel.py``), so a best-effort pre-audit would still let a model turn run
      unrecorded -- the audit would be a hope, not a gate. ``critical`` writes
      synchronously and re-raises, and the write is awaited through
      ``asyncio.to_thread`` because a synchronous flush on the loop would freeze
      every session's turn (``no-blocking-call-on-event-loop``). This is the same
      shape this subsystem's own ``autonudge_authz`` uses for ``monitor_update``
      and ``monitor_stop``, and the 503 mirrors ``handlers/cron.py``'s
      ``audit_unavailable`` refusal for a grant it could not record.
    * The **refusals** stay best-effort, but they are audited BEFORE
      ``fire_now`` returns: a guard below that denied a request and left no SEL
      event at all would be a real hole. Making them critical would trade an
      audit-sink problem for a different failure while preventing nothing: the
      request is refused either way, so availability must not hinge on SEL disk
      health. That is the disposition
      ``messaging/identity`` states for a deny and ``azure_client`` states for a
      post-action outcome.
    * The **terminal** event after ``fire_now`` is best-effort for the same
      reason: by then the timer is armed and the ``invoked`` record has landed,
      so raising would replace a real result with a logging error.

    Routing every exit through these two helpers is what makes the property
    structural rather than a habit: a guard added later cannot silently skip the
    record, because there is no un-audited way out.
    """
    # Read before the service check so the audit helpers can name the subject
    # even on the disabled path. Pure ``match_info`` read; no service needed.
    loop_id = request.match_info["loop_id"]

    async def _audit(outcome: str, session_key: str, error: str) -> None:
        """Best-effort record for an outcome that did NOT start a turn.

        OFF THE LOOP and failure-swallowing, both for stated reasons. The default
        SEL path only enqueues, which is cheap -- but ``sel()`` itself may lazily
        initialize the log, and on a degraded sink that initialization is
        filesystem work that would run on the gateway's event loop and stall
        every session (``no-blocking-call-on-event-loop``). And because this
        record accompanies a request that is being REFUSED, its own failure must
        not turn a clean 409 into a 500: the caller already learns the outcome
        from the status, so the audit is best-effort by contract here, exactly as
        the post-action outcome events are elsewhere in the codebase. The
        write-ahead ``invoked`` record is the one that fails closed.
        """
        try:
            await asyncio.to_thread(
                lambda: sel().log_tool_invocation(
                    session_key=session_key,
                    source="dashboard",
                    tool_name="autonudge_fire",
                    outcome=outcome,
                    metadata={
                        "loop_id": loop_id,
                        "caller": request.remote or "",
                        "error": error,
                    },
                )
            )
        except Exception:
            logger.warning(
                "autonudge fire: refusal audit unavailable (outcome=%s)",
                outcome,
                exc_info=True,
            )

    async def _audit_or_deny(session_key: str) -> bool:
        """Write-ahead audit for the fire. False = do not fire.

        ``sel()`` is resolved INSIDE the worker: on a fresh gateway the lookup
        lazily initializes the log, which is itself filesystem work that must not
        run on the event loop.
        """
        try:
            await asyncio.to_thread(
                lambda: sel().log_tool_invocation(
                    session_key=session_key,
                    source="dashboard",
                    tool_name="autonudge_fire",
                    outcome="invoked",
                    critical=True,
                    metadata={"loop_id": loop_id, "caller": request.remote or ""},
                )
            )
        except Exception:
            logger.error("autonudge fire denied: SEL audit unavailable", exc_info=True)
            return False
        return True

    svc = _autonudge_get()
    if svc is None:
        await _audit("denied", "", "autonudge_disabled")
        return web.json_response(
            {"error": "auto-nudge disabled", "code": "autonudge_disabled"},
            status=503,
        )
    existing = svc.get_by_id(loop_id)
    if existing is None:
        await _audit("denied", "", "autonudge_not_found")
        return web.json_response(
            {"error": "loop not found", "code": "autonudge_not_found"}, status=404
        )
    if is_structured_monitor_loop(existing):
        await _audit("denied", existing.slot_key, "structured_monitor_requires_monitor_api")
        return _monitor_error(
            "structured monitors must use the monitor update API",
            "structured_monitor_requires_monitor_api",
            status=409,
        )
    state: DashboardState = request.app["state"]
    slot = state.get_slot(existing.slot_key)
    if slot is not None and (slot.running or slot._in_stage_execution):
        # Names the OUTCOME and the NEXT STEP, not just the condition. "a turn is
        # in flight" leaves a reader unable to tell a refusal from a delay, and
        # the distinction is the whole point here: the press was refused, not
        # queued, so trying again later is the action. "still working" rather
        # than "mid-turn": a usability reader could only guess at the latter,
        # which is jargon from this codebase's vocabulary and not the user's.
        # Lowercase-first because all 13 error messages in this file are, and
        # this body is rendered verbatim beside them.
        await _audit("denied", existing.slot_key, "session_busy")
        return web.json_response(
            {
                "error": "nudge not sent: the agent is still working, so try again when it finishes",
                "code": "session_busy",
            },
            status=409,
        )
    if not await _audit_or_deny(existing.slot_key):
        # Fail closed, with nothing armed: the deadline has not moved and no
        # timer was re-armed, so the loop is exactly as the operator left it.
        return web.json_response(
            {
                "error": "audit log unavailable: the nudge was NOT sent, "
                "so fix the audit store and press again",
                "code": "audit_unavailable",
            },
            status=503,
        )
    loop, error, status = await svc.fire_now(loop_id)
    await _audit("success" if error == "" else "denied", existing.slot_key, error)
    if error:
        # Each arm carries a LITERAL status beside its code, rather than passing
        # ``status=status`` through. The error-code contract caps dynamic
        # statuses for a stated reason — computing one is how the coded-response
        # ratchet gets defeated while looking like ordinary refactoring — so the
        # pairing is written out where a reader and a static check can both see
        # it. Both arms are kept even though this route answers ``not found``
        # itself above: relying on the 404 being unreachable would make a later
        # edit to that guard silently change this response's status.
        if status == 404:
            return web.json_response({"error": error, "code": "autonudge_not_found"}, status=404)
        return web.json_response({"error": error, "code": "autonudge_not_fired"}, status=409)
    return web.json_response({"ok": True, "loop": _serialize(loop)})

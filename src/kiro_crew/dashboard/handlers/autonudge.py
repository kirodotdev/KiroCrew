"""Auto-nudge HTTP API — list / start / stop / update loops for chat slots."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.autonudge import scrub_loop_text  # noqa: F401 - re-exported
from kiro_crew.autonudge import ADDRESSING_FIELDS
from kiro_crew.autonudge import get_instance as _autonudge_get

# The security chokepoint lives in the transport-agnostic module (see its
# docstring); re-exported here so existing importers keep working. This file
# is intentionally a THIN HTTP mapping over it.
from kiro_crew.autonudge_authz import (  # noqa: F401 - re-exported
    authorize_and_add_nudge,
    authorize_and_update_nudge,
    message_is_echoed_projection,
    resolve_stop_sentinel,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.platform import redact_via_context
from kiro_crew.sel import sel
from kiro_crew.session_ledger import ledger_key, render_snapshot

logger = logging.getLogger(__name__)


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


#: Imported from the service rather than spelled again here: ``_load`` enforces
#: the invariant that makes this exemption safe (a persisted loop whose
#: addressing fields are credential-shaped is REFUSED), and two copies of the
#: set could drift so that the serializer exempts a field the loader does not
#: guard -- which is precisely the hole this exemption would then open.
_UNSCRUBBED_FIELDS = ADDRESSING_FIELDS


def _serialize(loop: Any) -> dict[str, Any]:
    """Serialize a loop for the REST surface, credential-scrubbing its text.

    ``asdict`` alone served ``message`` verbatim to every dashboard client. That is
    the same exposure ``_load`` and the transcript row already close, and this was
    the third surface. Three producers reach ``svc.add`` without the authorizer --
    the goal loop (``dashboard/chat_runner.py``), auto-research, and issue-radar,
    the last composing its message from external issue text -- and a hand-edited
    ``autonudge.json`` bypasses it too, so ``loop.message`` can hold text nothing
    has ever scanned.

    DENYLIST, not allowlist: every field is scrubbed unless named in
    ``_UNSCRUBBED_FIELDS``. An allowlist would silently miss the next free-text
    field added to ``NudgeLoop`` -- which is exactly how ``banner`` came to be covered
    here, by this same loop rather than by a scrub of its own, and ``stopped_reason`` is
    agent-supplied free text by the same route. So this is ONE rule serving every text
    field: ``message`` cannot be lifted out of it without either dropping the denylist,
    which un-scrubs ``banner`` too, or re-exempting ``message`` and restoring the verbatim
    leak. Redaction is shape-based and idempotent, so a value written through
    the authorizer, and any value with nothing credential-shaped in it, round-trips
    unchanged.

    NON-STRING VALUES ARE NOT SKIPPED. ``not isinstance(value, str)`` used to be an
    early-out, so an agent-written ``message: ["AKIA..."]`` was emitted verbatim to
    every dashboard client -- measured: the loop loaded and the payload carried the
    list intact. A store an agent writes directly has no type discipline, and the
    dataclass annotation is not enforced on ``NudgeLoop(**raw)``.

    ``monitor`` is the one field routed to a DIFFERENT redactor. It is structured
    nested state, so ``scrub_loop_text`` would take its non-scalar arm and
    ``str()``-flatten the whole mapping into one redacted string -- closing the same
    hole, but destroying the shape the dashboard parses. ``_redact_monitor_value``
    walks it instead, redacting every nested string key and value in place. So the
    denylist still covers every field; only the tool differs, chosen by the value's
    shape. Naming ``monitor`` in ``_UNSCRUBBED_FIELDS`` would have been the smaller
    edit and is wrong: that set is for fields ``_load`` REFUSES rather than scrubs,
    and monitor evidence is provider-controlled text with no such guard.

    The per-value rule lives in ``scrub_loop_text`` because the websocket broadcast
    needs the identical rule; see its docstring for why a declared scalar passes
    through untouched while anything else is redact-coerced. The ADDRESSING fields
    get the other half of that rule: ``_load`` REFUSES a non-string one rather than
    coercing it, because coercing the identity would leave a row the client cannot
    act on.
    """
    out = asdict(loop)
    for key, value in out.items():
        if key in _UNSCRUBBED_FIELDS:
            continue
        if key == "monitor":
            if value is not None:
                out[key] = _redact_monitor_value(value)
            continue
        out[key] = scrub_loop_text(value, field=key)
    # Tell the client when what it is being served DIFFERS from what is stored, so it can
    # know that echoing `message` back in a PATCH would destroy the original. Without it
    # the API's only answer to a read-modify-write was a silent server-side drop and a
    # 200, which no client can detect.
    out["message_redacted"] = out.get("message") != getattr(loop, "message", None)
    return out


async def api_autonudge_list(request: web.Request) -> web.Response:
    """GET /api/autonudge — list all active loops."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "loops": []})
    return web.json_response({"enabled": True, "loops": [_serialize(lp) for lp in svc.list_all()]})


async def api_autonudge_get(request: web.Request) -> web.Response:
    """GET /api/autonudge/{slot_key} — loop bound to this slot (or null)."""
    svc = _autonudge_get()
    slot_key = request.match_info["slot_key"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_slot(slot_key)
    return web.json_response({"enabled": True, "loop": _serialize(loop) if loop else None})


async def api_autonudge_start(request: web.Request) -> web.Response:
    """POST /api/autonudge — start or replace a loop on a slot.

    Body: { slot_key, message, idle_secs?, max_cycles?, max_runtime_secs?,
            stop_sentinel_path?, banner? }

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
        # and the redaction, so a non-string is a 400 from there rather than a
        # silent str() here that would persist "None" or "[1, 2]" as a banner.
        banner=body.get("banner"),
        source="dashboard",
        caller=request.remote or "",
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
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Decided BEFORE the update, against the stored value, using the SAME predicate the
    # authorizer's guard uses -- so the response cannot disagree with the behaviour.
    echoed = False
    with contextlib.suppress(Exception):
        current = svc.get_by_id(loop_id) if hasattr(svc, "get_by_id") else None
        echoed = message_is_echoed_projection(current, body.get("message"))
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
    # A 200 that silently discarded a field is a success-that-isn't, so name it. Keyed on
    # `echoed` -- the same boolean the guard uses -- because deriving it from the request
    # body instead reported a drop on a settings-only save, which submits no `message`.
    payload: dict[str, Any] = {"ok": True, "loop": _serialize(loop)}
    if echoed:
        payload["message_ignored"] = True
    return web.json_response(payload)


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
    existing = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    await svc.remove(loop_id)
    sel().log_tool_invocation(
        session_key=existing.slot_key if existing else "",
        source="dashboard",
        tool_name="autonudge_delete",
        outcome="success" if existing else "noop",
        metadata={"loop_id": loop_id, "caller": request.remote or ""},
    )
    return web.json_response({"ok": True})

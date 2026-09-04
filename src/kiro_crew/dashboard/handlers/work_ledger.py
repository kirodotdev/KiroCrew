"""HTTP routes for the conductor work ledger — the shared record between a
conductor session and the worker sessions it dispatched.

Thin mapping over :mod:`kiro_crew.work_ledger`, and the security contract is the
one :mod:`kiro_crew.dashboard.handlers.session_ledger` states: which ledger a
request touches is derived from the CALLING SESSION's identity
(``X-Session-Key``, vetted by ``_recognize_session``), never from the request
body. That is what makes the four tools' identity unforgeable — a worker has no
parameter naming its item, its conductor, or itself, so an out-of-bounds write is
unrepresentable rather than validated away.

Which half of the surface answers a call depends on what the caller RESOLVES to,
not on which spec mounted the tool:

===============================  =========================  ==========================
resolved caller                  ``brief`` / ``report``     ``read`` / ``record``
===============================  =========================  ==========================
a binding file, no ledger dir    available                  ``no_ledger`` (404)
a ledger dir, no binding file    ``not_bound`` (403)        available
both — a second-level conductor  available                  available
neither                          ``not_bound`` (403)        ``no_ledger`` (404)
===============================  =========================  ==========================

All four routes are MCP-only (no browser caller) and listed under
``server._STRICT_INTERNAL_API_PATHS`` by their shared ``/api/work-ledger``
prefix — without that entry the internal-secret call falls through to cookie auth
and every tool call fails with 403 before this module's own recognition can run.

Restricted (incognito / temporary / guest) sessions are refused: a ledger is
durable on-disk state, which is exactly what those modes promise not to leave
behind.

One spelling note that is load-bearing. Every key — the caller's own, and the
``worker_session_key`` a conductor supplies at ``bind`` — is folded through
:func:`session_ledger.ledger_key` before it reaches the store. One dashboard
session is legitimately spelled both ``dashboard_chat-X`` and ``chat-X``, so
without the fold a conductor could bind the spelling ``session_create`` returned
while the worker resolves the other one, and the worker would read ``not_bound``
against a binding that exists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import session_ledger, work_ledger
from kiro_crew.dashboard import session_control
from kiro_crew.dashboard.handlers._shared import _is_restricted_session

# Module-scope like ``session_ledger.py``'s identical imports: the recognition
# gate and the incognito classifier are this module's own load-bearing deps.
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import is_incognito_transcript
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.validation import (
    WORK_LEDGER_RECORD_SCHEMA,
    WORK_REPORT_SCHEMA,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.work_ledger import WorkLedgerError

logger = logging.getLogger(__name__)

#: Every store error code, mapped to the status the RFC tabulates for it. A code
#: absent from this map is a store invariant this layer has not been taught, and
#: it degrades to 400 rather than to 500: the codes are all caller-input
#: failures, so a new one is far likelier to be a bad argument than a server
#: fault. The map is asserted exhaustive against the ``CODE_*`` constants by
#: ``test_work_ledger_tools.py``, so a new code cannot arrive here unnoticed.
_CODE_STATUS: dict[str, int] = {
    work_ledger.CODE_NO_LEDGER: 404,
    work_ledger.CODE_UNKNOWN_ITEM: 404,
    work_ledger.CODE_ALREADY_BOUND: 409,
    work_ledger.CODE_ITEM_CLOSED: 409,
    work_ledger.CODE_ITEM_CAP_EXCEEDED: 409,
    work_ledger.CODE_DEPTH_EXCEEDED: 409,
    work_ledger.CODE_FIELD_TOO_LONG: 400,
    work_ledger.CODE_INVALID_ACTION: 400,
    work_ledger.CODE_INVALID_STATUS: 400,
    work_ledger.CODE_INVALID_VALUE: 400,
}

#: Codes this LAYER owns, above the store's own. Each names a condition the store
#: cannot see: who the caller is on the wire, and which sessions it created.
#: Deliberately not folded into :data:`_CODE_STATUS` — that map is asserted
#: exhaustive against the store's ``CODE_*`` constants, and adding a route-only code
#: to it would break the property that makes the assertion meaningful.
ROUTE_CODES: frozenset[str] = frozenset(
    {
        "restricted_session",
        "internal_auth_required",
        "channel_session",
        "parent_unreadable",
        "unknown_worker_session",
        "worker_not_owned",
        "worker_already_dispatched",
        "worker_cross_workspace",
        "invalid_json",
        "invalid_body",
        "ledger_write_failed",
    }
)

#: Refused because the caller has no binding file. Its own code, distinct from
#: ``no_ledger``, because the two answer different questions about the same
#: session and a caller that is neither must be able to tell which half it is
#: missing.
CODE_NOT_BOUND = "not_bound"

#: The actions ``work_ledger_record`` accepts. A SUPERSET of the store's
#: :data:`work_ledger.CONDUCTOR_ACTIONS`: ``accept`` promotes a worker's claimed
#: ``pr`` into the item's ``acceptance`` and is served by
#: :func:`work_ledger.apply_acceptance_update`, which is deliberately not a
#: seventh member of that frozenset (see its docstring).
RECORD_ACTIONS: frozenset[str] = work_ledger.CONDUCTOR_ACTIONS | {"accept"}


def _sel():
    """Late-binding ``sel()`` for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _audit(caller: str, operation: str, outcome: str, resources: str = "", error: str = "") -> None:
    """Enqueue one SEL row for a ledger touch.

    A bare enqueue, NOT wrapped in ``asyncio.to_thread``: SEL is warmed at
    gateway startup, so the first-touch filesystem initialization off-loading
    would protect against never runs here. Guarded because a FAILED
    warm leaves construction to retry on this thread and possibly raise, and the
    audit must never change the route's outcome.
    """
    try:
        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


# One sink per status, each a LITERAL ``status=`` over a LITERAL body dict whose
# ``code`` key is spelled out. Both halves are deliberate rather than verbose: the
# error-code contract gate resolves the status and the body statically, so a helper
# taking the status as a parameter, or a body built by another function, is
# indistinguishable to it from the hoisting it exists to catch — and every refusal
# on this surface must be provably coded, because the dashboard renders ``error``
# verbatim into a localized UI while ``code`` is the contract.
#
# ``field`` rides on every body, null where there is none. A conditional insert
# needs a ``**spread``, which the gate must read as opaque because ``code`` itself
# could arrive through it.


def _refuse_400(code: str, message: str, field: str | None = None) -> web.Response:
    return web.json_response({"error": message, "code": code, "field": field}, status=400)


def _refuse_403(code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code, "field": None}, status=403)


def _refuse_404(code: str, message: str, field: str | None = None) -> web.Response:
    return web.json_response({"error": message, "code": code, "field": field}, status=404)


def _refuse_409(code: str, message: str, field: str | None = None) -> web.Response:
    return web.json_response({"error": message, "code": code, "field": field}, status=409)


def _refuse_503(code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code, "field": None}, status=503)


def _refuse_store_error(exc: WorkLedgerError) -> web.Response:
    """Map a store refusal onto its tabulated status, naming the field it bounded.

    The status comes from :data:`_CODE_STATUS`, but it is DISPATCHED to a literal
    sink rather than passed as a value, for the reason above.
    """
    status = _CODE_STATUS.get(exc.code, 400)
    if status == 404:
        return _refuse_404(exc.code, str(exc), exc.field)
    if status == 409:
        return _refuse_409(exc.code, str(exc), exc.field)
    return _refuse_400(exc.code, str(exc), exc.field)


async def _caller_key(
    request: web.Request, operation: str
) -> tuple[str, None] | tuple[None, web.Response]:
    """Vet the calling session and fold its key to the ledger spelling.

    Returns ``(key, None)`` or ``(None, refusal)``. Identical in shape and in
    reasoning to ``session_ledger._resolve_ledger_key``: the recognition gate
    decides whether this key names a session at all, and the fold is the lossless
    dashboard-prefix strip so one session's two legitimate spellings reach one
    record while two distinct channel keys can never collide.
    """
    if not request.get("internal_auth"):
        # The internal-secret principal, positively confirmed — NOT inferred from
        # the path being in ``_STRICT_INTERNAL_API_PATHS``. That table denies a
        # NON-loopback cookie caller outright, but on loopback a strict path with
        # no secret header falls through to cookie auth and is GRANTED, reaching
        # this handler with ``internal_auth`` unset. ``_recognize_session`` then
        # accepts any ``X-Session-Key`` naming a known session and never ties it to
        # the authenticated principal, so without this check a cookie-authed
        # loopback caller could name ANOTHER session's key and read or write its
        # ledger. The unix-socket peer check that makes the header unforgeable for
        # MCP callers does not run on TCP loopback.
        #
        # Costs nothing reachable: all four routes are MCP-only by design (the
        # module docstring says so, and no browser code calls them). A future Crew
        # page reader is a browser caller and needs its own route with its own
        # identity rule — it must not arrive by relaxing this one.
        _audit(
            request.headers.get("X-Session-Key", "") or "anonymous",
            operation,
            "denied",
            resources="cookie_caller_block",
            error="internal_auth_required",
        )
        return None, _refuse_403(
            "internal_auth_required",
            "The work ledger is reachable only by an agent's MCP tools, which "
            "authenticate with the gateway's internal secret. A browser session "
            "cannot name which ledger it is.",
        )
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, operation, blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return None, refusal
    if _is_restricted_session(state, request):
        _audit(
            sk,
            operation,
            "denied",
            resources="restricted_session_block",
            error="Work-ledger access is not allowed in this session mode.",
        )
        return None, _refuse_403(
            "restricted_session",
            "The work ledger is not available in this session mode.",
        )
    if is_channel_session_key(sk) or _reaches_a_channel(request, sk):
        # Containment for channel agents, held HERE rather than only in
        # ``channel.CHANNEL_AGENT_BLOCKED_TOOLS``. That list is matched against a
        # rendered permission request (``channel.py``'s ``EVENT_PERMISSION_REQUEST``
        # arm), so a tool auto-approved through ``allowedTools`` emits no permission
        # event and the block never runs — which is exactly the bypass an
        # ``autoApprove`` key would open, and the reason this server carries none.
        # The four tools grant themselves per-tool auto-approve on three specs, so
        # the block alone is not sufficient for them and the refusal has to be
        # server-side, where no spec and no client can route around it.
        #
        # Nothing reachable is lost. A conductor cannot be a channel session:
        # ``session_control._refuse_ineligible_creator`` refuses a channel-bound
        # caller, so such a session can never dispatch a worker and never owns
        # items. And a worker is dispatched BY ``session_create``, which mints a
        # dashboard slot — never a channel key. So a channel caller here is either
        # a misconfiguration or the containment case. The same holds for a
        # dashboard-BORN session that was later given an outbound mirror: its key
        # looks local while every turn is republished to Slack or Telegram, so the
        # key alone is not the test — see :func:`_reaches_a_channel`.
        _audit(sk, operation, "denied", resources="channel_agent_block")
        return None, _refuse_403(
            "channel_session",
            "The work ledger is not reachable from a channel session. A channel "
            "agent has no dispatch relationship: it can neither create the worker "
            "sessions a conductor binds nor be one.",
        )
    return session_ledger.ledger_key(sk), None


def _reaches_a_channel(request: web.Request, sk: str) -> bool:
    """Whether this caller's turns reach a messaging channel, mirror included.

    ``is_channel_session_key`` catches a channel-BORN session. It does not catch a
    dashboard-born one that was later given an OUTBOUND mirror link — the link
    lives in the session store, not in the key, so a plain ``chat-*`` key can still
    be republishing every turn to a channel. A brief carries the acceptance bar of
    a private dispatch and the ledger carries worker-authored prose, so the mirror
    is the same disclosure as a channel key and gets the same refusal.

    Delegates to ``session_control._has_channel_mirror``, which is the same
    predicate ``_refuse_ineligible_creator`` applies to a session asking to create
    a peer, and which FAILS CLOSED: a session store that cannot answer counts as
    mirrored.
    """
    state: DashboardState = request.app["state"]
    for candidate in (
        sk,
        session_ledger.ledger_key(sk),
        f"dashboard_{session_ledger.ledger_key(sk)}",
    ):
        try:
            slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover - a slot read must not 500
            logger.debug("slot lookup failed for %s", candidate, exc_info=True)
            continue
        if slot is None:
            continue
        try:
            if session_control._has_channel_mirror(state, slot):
                return True
        except Exception:  # pragma: no cover
            # Fail CLOSED, matching the helper's own default: an unreadable link is
            # treated as mirrored rather than opening the boundary.
            logger.debug("channel-mirror probe failed for %s", candidate, exc_info=True)
            return True
    return False


async def _worker_binding(
    key: str, operation: str
) -> tuple[tuple[str, str], None] | tuple[None, web.Response]:
    """Resolve the caller's own binding, or refuse with ``not_bound``.

    The whole of a worker's addressing: the conductor key and item id come from
    ``bindings/<the caller's digest>.json`` and from nowhere else, which is why
    neither worker tool has a parameter that could name either.
    """
    binding = await asyncio.to_thread(work_ledger.read_binding, key)
    if binding is None:
        _audit(key, operation, "denied", error=CODE_NOT_BOUND)
        return None, _refuse_403(
            CODE_NOT_BOUND,
            "This session is not bound to a work item, so it has no brief to read "
            "or report against. A conductor binds an item to a worker session; "
            "only that session can use the worker tools.",
        )
    return binding, None


async def _own_ledger(
    key: str, operation: str
) -> tuple[work_ledger.ConductorRecord, None] | tuple[None, web.Response]:
    """Resolve the caller's own conductor record, or refuse with ``no_ledger``."""
    record = await asyncio.to_thread(work_ledger.read_conductor, key)
    if record is None:
        _audit(key, operation, "denied", error=work_ledger.CODE_NO_LEDGER)
        return None, _refuse_404(
            work_ledger.CODE_NO_LEDGER,
            "This session owns no work ledger. Start one with "
            "work_ledger_record action=goal (or action=create) before reading it.",
        )
    return record, None


# ── worker half ───────────────────────────────────────────────────────────


async def api_work_brief(request: web.Request) -> web.Response:
    """GET /api/work-ledger/brief — the ONE item this session was dispatched for.

    Title, acceptance, round, the conductor's latest decision, and this worker's
    own last status/summary. Deliberately not the conductor's goal and not a
    sibling's anything: a worker has no reason to see its peers.
    """
    key, refusal = await _caller_key(request, "work_brief")
    if refusal is not None:
        return refusal
    assert key is not None
    binding, brefusal = await _worker_binding(key, "work_brief")
    if brefusal is not None:
        return brefusal
    assert binding is not None
    conductor_key, item_id = binding
    brief = await asyncio.to_thread(work_ledger.read_work_brief, conductor_key, item_id)
    if _reaches_a_channel(request, request.headers.get("X-Session-Key", "")):
        # Re-checked AFTER the await, immediately before the data would be
        # returned. An outbound mirror can be added at any moment, so containment
        # decided on entry says nothing about containment now — the same reason
        # ``session_control`` applies ``_refuse_ineligible_creator`` twice, once on
        # entry and again just before it allocates. A brief carries a private
        # dispatch's acceptance bar, and a mirrored reply publishes it.
        _audit(
            request.headers.get("X-Session-Key", "") or "anonymous",
            "work_brief",
            "denied",
            resources="channel_agent_block_post_read",
        )
        return _refuse_403(
            "channel_session",
            "This session gained a channel mirror while the brief was being read, "
            "so it is no longer a private surface to return it to.",
        )
    if brief is None:
        # The binding names an item that is gone or unreadable. 404 on the ITEM,
        # not 403 on the binding: the caller IS bound, and telling it otherwise
        # would send a worker looking for a grant it already has.
        _audit(key, "work_brief", "denied", resources=item_id, error=work_ledger.CODE_UNKNOWN_ITEM)
        return _refuse_404(
            work_ledger.CODE_UNKNOWN_ITEM,
            f"the item this session is bound to ({item_id}) is not readable",
        )
    _audit(key, "work_brief", "ok", resources=item_id)
    return web.json_response({"brief": brief})


async def api_work_report(request: web.Request) -> web.Response:
    """POST /api/work-ledger/report — this worker's status against its own item.

    ``status`` and ``summary`` are required; ``artifacts`` and ``pr`` are
    optional. There is no ``item_id``, no ``session``, no ``acceptance``, no
    ``verdict`` and no ``state``, so no round trip through here can write a
    conductor-owned field.
    """
    key, refusal = await _caller_key(request, "work_report")
    if refusal is not None:
        return refusal
    assert key is not None
    binding, brefusal = await _worker_binding(key, "work_report")
    if brefusal is not None:
        return brefusal
    assert binding is not None
    conductor_key, item_id = binding

    body, bad = await _json_object(request)
    if bad is not None:
        return bad
    assert body is not None
    try:
        cleaned = validate_tool_args(_drop_nulls(body), WORK_REPORT_SCHEMA)
    except ValidationError as exc:
        return _refuse_400(_validation_code(exc), str(exc))

    try:
        result = await asyncio.to_thread(
            _report,
            conductor_key,
            item_id,
            cleaned.get("status"),
            cleaned.get("summary"),
            cleaned.get("artifacts"),
            cleaned.get("pr"),
        )
    except WorkLedgerError as exc:
        _audit(key, "work_report", "denied", resources=item_id, error=exc.code)
        return _refuse_store_error(exc)
    except OSError:
        logger.warning("work ledger report failed for %s", item_id, exc_info=True)
        return _refuse_503("ledger_write_failed", "ledger write failed; try again")

    item = result["item"]
    _audit(key, "work_report", "ok", resources=f"{item_id} status={item.status}")
    return web.json_response(
        {"ok": True, "item_id": item.item_id, "status": item.status, "round": item.round}
    )


def _report(
    conductor_key: str,
    item_id: str,
    status: Any,
    summary: Any,
    artifacts: Any,
    pr: Any,
) -> dict[str, Any]:
    return work_ledger.apply_worker_report(
        conductor_key,
        item_id,
        status=status,
        summary=summary,
        artifacts=artifacts,
        pr=pr,
    )


# ── conductor half ────────────────────────────────────────────────────────


async def api_work_ledger_get(request: web.Request) -> web.Response:
    """GET /api/work-ledger — the whole ledger this session owns.

    The conductor record, every item with all its fields, the derived
    ``orphaned`` / ``stale`` flags, each item's newest events, and a
    ready-to-pipe ``accept_batch`` built from ``acceptance`` ALONE — never from a
    worker's claimed ``pr``, which is surfaced beside the item instead.
    """
    key, refusal = await _caller_key(request, "work_ledger_read")
    if refusal is not None:
        return refusal
    assert key is not None
    record, lrefusal = await _own_ledger(key, "work_ledger_read")
    if lrefusal is not None:
        return lrefusal
    assert record is not None

    state: DashboardState = request.app["state"]
    items = await asyncio.to_thread(work_ledger.list_work_items, key)
    # Liveness is read straight off the dashboard's own slot table rather than
    # over HTTP: this handler runs in the process that owns it. ``orphaned`` asks
    # whether the CONDUCTOR's slot is still open and ``stale`` whether the
    # WORKER's is — the conjunction with the staleness window is what keeps a
    # worker in a thirty-minute build from being flagged.
    conductor_alive = _slot_open(state, key)
    rows: list[dict[str, Any]] = []
    for item in items:
        row = item.to_dict()
        row["orphaned"] = work_ledger.is_orphaned(item, conductor_slot_exists=conductor_alive)
        row["stale"] = work_ledger.is_stale(
            item, worker_running=_slot_running(state, item.worker_session_key or "")
        )
        events = await asyncio.to_thread(_tail_events, key, item.item_id, _MAX_EVENT_TAIL)
        row["events"] = [event.to_dict() for event in events]
        rows.append(row)

    if _reaches_a_channel(request, request.headers.get("X-Session-Key", "")):
        # Same post-await re-check as ``work_brief``. This payload is larger: every
        # item's acceptance bar plus worker-authored prose for the whole fleet.
        _audit(
            request.headers.get("X-Session-Key", "") or "anonymous",
            "work_ledger_read",
            "denied",
            resources="channel_agent_block_post_read",
        )
        return _refuse_403(
            "channel_session",
            "This session gained a channel mirror while the ledger was being read, "
            "so it is no longer a private surface to return it to.",
        )
    _audit(key, "work_ledger_read", "ok", resources=f"{len(rows)} item(s)")
    return web.json_response(
        {
            "conductor": record.to_dict(),
            "items": rows,
            "accept_batch": work_ledger.accept_batch(items),
        }
    )


#: Events returned per item. The log is append-only and capped at 200 per item,
#: so an unbounded slice would eventually be the largest thing in a conductor's
#: context — the opposite of what a patrol cycle needs. Newest kept, oldest
#: dropped, which is the slice ``read_events``' own ``limit`` already applies.
_MAX_EVENT_TAIL = 20


def _tail_events(key: str, item_id: str, limit: int) -> list[work_ledger.WorkEvent]:
    """The newest *limit* events for one item. ``limit`` is keyword-only downstream."""
    return work_ledger.read_events(key, item_id, limit=limit)


def _slot_running(state: DashboardState, key: str) -> bool:
    """Whether *key*'s slot has a TURN IN FLIGHT — not merely an open tab.

    ``stale`` is the conjunction "quiet past the window AND not running", and the
    second half means the worker is doing something (a thirty-minute build), not
    that its session exists. An idle worker whose tab is still open but which
    stopped without reporting is exactly the case the flag exists to surface, and
    testing slot EXISTENCE here would never flag it. ``orphaned`` keeps the
    existence test, because a conductor's absence is what that flag means.
    """
    slot = _find_slot(state, key)
    return bool(getattr(slot, "running", False)) if slot is not None else False


def _find_slot(state: DashboardState, key: str):
    if not key:
        return None
    for candidate in (key, f"dashboard_{key}"):
        try:
            slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover - a slot-table read must not fail a read
            logger.debug("slot lookup failed for %s", candidate, exc_info=True)
            continue
        if slot is not None:
            return slot
    return None


def _slot_open(state: DashboardState, key: str) -> bool:
    """Whether *key* still names an open slot. A blank key is never alive."""
    if not key:
        return False
    try:
        return state.get_slot(key) is not None or state.get_slot(f"dashboard_{key}") is not None
    except Exception:  # pragma: no cover - a slot-table read must not fail a read
        logger.debug("slot liveness read failed for %s", key, exc_info=True)
        return False


async def api_work_ledger_record(request: web.Request) -> web.Response:
    """POST /api/work-ledger/record — one conductor-owned write.

    An ``action`` selects the operation because the field sets are disjoint and
    one flat schema would accept nonsense combinations. ``goal`` and ``create``
    also BOOTSTRAP the ledger, which is the only way one comes into existence;
    every other action answers ``no_ledger`` until then, so a conductor cannot
    bind or decide against a ledger that was never opened.
    """
    key, refusal = await _caller_key(request, "work_ledger_record")
    if refusal is not None:
        return refusal
    assert key is not None

    body, bad = await _json_object(request)
    if bad is not None:
        return bad
    assert body is not None
    try:
        cleaned = validate_tool_args(_drop_nulls(body), WORK_LEDGER_RECORD_SCHEMA)
    except ValidationError as exc:
        return _refuse_400(_validation_code(exc), str(exc))

    action = str(cleaned.get("action") or "")
    if action not in RECORD_ACTIONS:
        return _refuse_400(
            work_ledger.CODE_INVALID_ACTION,
            f"unknown action {action!r}; expected one of: {', '.join(sorted(RECORD_ACTIONS))}",
        )

    if action == "bind":
        refusal = _refuse_unowned_worker(request, key, cleaned.get("worker_session_key"))
        if refusal is not None:
            return refusal

    if action in ("goal", "create"):
        bootstrapped = await _bootstrap(key)
        if isinstance(bootstrapped, web.Response):
            return bootstrapped
    else:
        _record, lrefusal = await _own_ledger(key, "work_ledger_record")
        if lrefusal is not None:
            return lrefusal

    try:
        result = await asyncio.to_thread(_write, key, action, cleaned)
    except WorkLedgerError as exc:
        _audit(key, "work_ledger_record", "denied", resources=action, error=exc.code)
        return _refuse_store_error(exc)
    except OSError:
        logger.warning("work ledger write failed for %s", key, exc_info=True)
        return _refuse_503("ledger_write_failed", "ledger write failed; try again")

    item = result.get("item")
    _audit(
        key,
        "work_ledger_record",
        "ok",
        resources=f"{action} {getattr(item, 'item_id', '') or ''}".strip(),
    )
    payload: dict[str, Any] = {"ok": True, "action": action}
    if item is not None:
        payload["item"] = item.to_dict()
    conductor = result.get("conductor")
    if conductor is not None:
        payload["conductor"] = conductor.to_dict()
    return web.json_response(payload)


def _has_binding(worker_key: str) -> bool:
    """Whether this worker session has EVER been bound. Fails closed.

    An unreadable binding counts as present: the alternative is admitting a rebind
    on a store that could not answer, which is the case the refusal exists for.
    """
    try:
        return work_ledger.binding_path(worker_key).exists()
    except Exception:  # pragma: no cover - a path/store error must not admit a bind
        logger.debug("binding presence check failed for %s", worker_key, exc_info=True)
        return True


def _refuse_unowned_worker(
    request: web.Request, conductor_key: str, worker_session_key: Any
) -> web.Response | None:
    """Refuse a ``bind`` whose worker session this conductor does not own.

    The store checks only that the ITEM is unbound and that the worker does not
    already hold an open item — neither of which says the worker is *this*
    conductor's. Without this, conductor A can bind conductor B's idle worker
    session to A's item, and B's worker then reads A's brief and A's ``decision``
    field, which is the one field a worker treats as an instruction. That is
    cross-session control, and it is the same class the strict identity resolver
    exists to prevent from the other direction.

    Ownership is ``session_create``'s own attribution: it stamps ``_created_by``
    with the calling session's key inside the synchronous window after the mint,
    and it is the only entry point that does — a person's own tab stays
    unattributed. So a bind is admitted only for a live slot this conductor
    created, in the conductor's own workspace (the memory boundary
    ``authorize_target`` already refuses across).

    Returns a refusal, or ``None`` when the bind may proceed.
    """
    if not isinstance(worker_session_key, str) or not worker_session_key:
        return None  # the store's own invalid_value refusal is the better message
    state: DashboardState = request.app["state"]
    folded = session_ledger.ledger_key(worker_session_key)
    slot = None
    for candidate in (worker_session_key, folded, f"dashboard_{folded}"):
        try:
            slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover - a slot-table read must not 500
            logger.debug("slot lookup failed for %s", candidate, exc_info=True)
            slot = None
        if slot is not None:
            break
    if slot is None:
        _audit(
            conductor_key,
            "work_ledger_record",
            "denied",
            resources="bind",
            error="unknown_worker_session",
        )
        return _refuse_404(
            "unknown_worker_session",
            "That worker session is not open, so it cannot be bound. Create the "
            "session first and bind the key session_create returned — binding "
            "before the session exists is what leaves a worker running unbound.",
        )
    if _has_binding(folded):
        # ONE binding per worker session, ever. The store permits replacing a
        # binding whose item is terminal, which is how a session could be reused —
        # but a report already in flight from the old work resolves its binding when
        # it LANDS, so it would write the new item's status, summary, artifacts and
        # pr, and there is no unbind path to undo it.
        #
        # An idle check on the worker slot does not guard that: it is a TOCTOU,
        # since a turn can start during the awaits between it and the commit, and
        # closing THAT would mean making turn admission and binding replacement
        # atomic — a coordination mechanism across two subsystems, for a
        # capability nothing needs. The RFC's lifecycle dispatches one session per
        # item, so refusing reuse costs a conductor one ``session_create`` and makes
        # the race UNREPRESENTABLE rather than guarded. The store keeps its
        # staleness allowance for a future explicit unbind.
        _audit(
            conductor_key,
            "work_ledger_record",
            "denied",
            resources="bind",
            error="worker_already_dispatched",
        )
        return _refuse_409(
            "worker_already_dispatched",
            "That worker session has already been bound to a work item. A session "
            "is dispatched for one item; create a new session for this one.",
        )
    creator = session_ledger.ledger_key(str(getattr(slot, "_created_by", "") or ""))
    if creator != conductor_key:
        _audit(
            conductor_key,
            "work_ledger_record",
            "denied",
            resources="bind",
            error="worker_not_owned",
        )
        return _refuse_403(
            "worker_not_owned",
            "That worker session was not created by this conductor, so it cannot "
            "be bound to one of its items. A conductor binds only the sessions it "
            "dispatched.",
        )
    conductor_slot = None
    for candidate in (conductor_key, f"dashboard_{conductor_key}"):
        try:
            conductor_slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover
            conductor_slot = None
        if conductor_slot is not None:
            break
    if conductor_slot is not None:
        mine = str(getattr(conductor_slot, "workspace", "default") or "default")
        theirs = str(getattr(slot, "workspace", "default") or "default")
        if mine != theirs:
            _audit(
                conductor_key,
                "work_ledger_record",
                "denied",
                resources="bind",
                error="worker_cross_workspace",
            )
            return _refuse_403(
                "worker_cross_workspace",
                "That worker session is in a different workspace. Workspace is the "
                "memory boundary, and a conductor cannot bind across it.",
            )
    return None


async def _bootstrap(key: str) -> None | web.Response:
    """Open this session's ledger if it has none, deriving depth from its parent.

    A session that is ITSELF bound to an item is a second-level conductor, so its
    depth is one past its parent's and its ``parent_item`` is the item it was
    dispatched for. :func:`work_ledger.child_depth` refuses past the cap, which
    is how a grandchild is stopped from conducting — the refusal surfaces as
    ``depth_exceeded`` (409) at the moment the child tries to open a ledger,
    rather than later when it tries to create an item.
    """
    depth = 0
    parent_item: str | None = None
    binding = await asyncio.to_thread(work_ledger.read_binding, key)
    if binding is not None:
        parent_key, parent_item = binding
        parent = await asyncio.to_thread(work_ledger.read_conductor, parent_key)
        if parent is None:
            # REFUSE rather than assume depth 0. ``read_conductor`` answers ``None``
            # for an absent record AND for a torn or unreadable one, and treating
            # that as "no parent" would compute ``child_depth(0) == 1`` for a child
            # whose parent is really at depth 1 — then PERSIST that 1, granting one
            # extra generation that no later read corrects. A cap that fails open on
            # an unreadable input is not a cap, so this is the one place the
            # ledger's read-as-absent convention must not be inherited.
            _audit(
                key,
                "work_ledger_record",
                "denied",
                resources="bootstrap",
                error="parent_unreadable",
            )
            return _refuse_409(
                "parent_unreadable",
                "This session is bound to a work item, but its conductor's own "
                "ledger is not readable, so the nesting depth of a ledger opened "
                "here cannot be established. Retry once the parent's record is "
                "readable.",
            )
        try:
            depth = work_ledger.child_depth(parent.depth)
        except WorkLedgerError as exc:
            _audit(key, "work_ledger_record", "denied", resources="bootstrap", error=exc.code)
            return _refuse_store_error(exc)
    try:
        await asyncio.to_thread(_ensure, key, depth, parent_item if binding is not None else None)
    except WorkLedgerError as exc:
        _audit(key, "work_ledger_record", "denied", resources="bootstrap", error=exc.code)
        return _refuse_store_error(exc)
    except OSError:
        logger.warning("work ledger bootstrap failed for %s", key, exc_info=True)
        return _refuse_503("ledger_write_failed", "ledger write failed; try again")
    return None


def _ensure(key: str, depth: int, parent_item: str | None) -> work_ledger.ConductorRecord:
    return work_ledger.ensure_conductor(key, depth=depth, parent_item=parent_item)


def _write(key: str, action: str, cleaned: dict[str, Any]) -> dict[str, Any]:
    """Route one validated action to the store call that owns it."""
    if action == "accept":
        return work_ledger.apply_acceptance_update(
            key,
            str(cleaned.get("item_id") or ""),
            acceptance=cleaned.get("acceptance"),
        )
    # ``worker_session_key`` is folded exactly as the caller's own key is, so the
    # digest the conductor binds is the digest the worker resolves to.
    worker_key = cleaned.get("worker_session_key")
    if isinstance(worker_key, str) and worker_key:
        worker_key = session_ledger.ledger_key(worker_key)
    return work_ledger.apply_conductor_action(
        key,
        action,
        item_id=cleaned.get("item_id") or None,
        title=cleaned.get("title"),
        acceptance=cleaned.get("acceptance"),
        worker_session_key=worker_key,
        decision=cleaned.get("decision"),
        verdict=cleaned.get("verdict"),
        state=cleaned.get("state"),
        goal=cleaned.get("goal"),
        round_number=cleaned.get("round"),
        fails=cleaned.get("fails"),
    )


# ── shared request plumbing ───────────────────────────────────────────────


async def _json_object(
    request: web.Request,
) -> tuple[dict[str, Any], None] | tuple[None, web.Response]:
    try:
        body = await request.json()
    except Exception:
        return None, _refuse_400("invalid_json", "invalid JSON")
    if not isinstance(body, dict):
        return None, _refuse_400("invalid_body", "request body must be a JSON object")
    return body, None


def _drop_nulls(body: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the caller left null, and NOTHING else.

    Deliberately not ``session_ledger``'s drop-unknown-keys pre-filter. An unknown
    key here must be REFUSED, because the guarantee this surface rests on is that a
    worker has no parameter naming another item — and a silently dropped
    ``item_id`` answers 200, which tells a worker its write landed where it aimed
    it. The tool layer forwards only each schema's own fields, so a key that is
    unknown at this point is never a legitimate caller.

    A ``None`` is dropped rather than validated so an omitted optional field and an
    explicit null mean the same thing to the store.
    """
    return {k: v for k, v in body.items() if v is not None}


def _validation_code(exc: ValidationError) -> str:
    """The store's own code for a schema refusal, so one vocabulary reaches the model.

    A length overrun is the store's ``field_too_long`` and an out-of-vocabulary
    ``status`` is its ``invalid_status`` whether the bound was checked here or one
    layer down; reporting a generic ``validation_error`` for the same condition
    would make the caller handle two names for one thing.
    """
    message = getattr(exc, "message", "") or ""
    if "exceeds max length" in message or "exceeds max items" in message:
        return work_ledger.CODE_FIELD_TOO_LONG
    if getattr(exc, "field", "") == "status":
        return work_ledger.CODE_INVALID_STATUS
    if getattr(exc, "field", "") == "action":
        return work_ledger.CODE_INVALID_ACTION
    return work_ledger.CODE_INVALID_VALUE

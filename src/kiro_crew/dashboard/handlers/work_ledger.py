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
import functools
import logging
from typing import Any

from aiohttp import web

from kiro_crew import session_ledger, work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.resolve import UNKNOWN, unit_for_session_key
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
from kiro_crew.work_vocab import WORK_CONDUCTOR_FIELDS

logger = logging.getLogger(__name__)

#: Every store error code, mapped to the status the RFC tabulates for it. A code
#: absent from this map is a store invariant this layer has not been taught, and
#: it degrades to 400 rather than to 500: the codes are all caller-input
#: failures, so a new one is far likelier to be a bad argument than a server
#: fault. The map is asserted exhaustive against the ``CODE_*`` constants by
#: ``test_work_ledger_tools.py``, so a new code cannot arrive here unnoticed.
_CODE_STATUS: dict[str, int] = {
    work_ledger.CODE_CREW_LOG_INCOMPLETE: 409,
    work_ledger.CODE_CACHE_DIRTY: 409,
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
    # Raised only by ``work_ledger.purge_conductor``, the maintenance primitive
    # behind ``kirocrew ledger-sweep``, which no route calls. Mapped
    # anyway because this map is asserted EXHAUSTIVE over the store's ``CODE_*``
    # constants, and that property is what stops a code the store gains later
    # from degrading to 400 unnoticed. 409 is its class: the ledger is not in a
    # state where the operation is allowed, exactly like ``item_closed``.
    work_ledger.CODE_LEDGER_NOT_FINISHED: 409,
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
        "crew_log_off",
        "crew_log_unit_unknown",
        "crew_log_unreadable",
        "crew_log_unrecorded",
        "work_entry_too_large",
        "work_item_too_large",
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
    # Under the board lock, like every write: a write commits to the cache and
    # then appends to the record, undoing the commit if the append fails, and a
    # rebuild rewrites the cache in place. A read between those steps would serve
    # a state the record never holds. The lock makes the read see either side.
    async with _board_lock(conductor_key):
        dirty = await _refuse_if_dirty(conductor_key, key, "work_brief")
        if dirty is not None:
            return dirty
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
    dirty = await _refuse_if_dirty(conductor_key, key, "work_report")
    if dirty is not None:
        return dirty

    state: DashboardState = request.app["state"]
    unit, urefusal = _acting_unit(state, key, "work_report", item_id)
    if urefusal is not None:
        return urefusal
    assert unit is not None
    # The closure below reads these; a nested function does not inherit the
    # ``is not None`` narrowing, so it takes them as ``str`` bindings.
    caller: str = key
    acting_unit: str = unit

    body, bad = await _json_object(request)
    if bad is not None:
        return bad
    assert body is not None
    try:
        cleaned = validate_tool_args(_drop_nulls(body), WORK_REPORT_SCHEMA)
    except ValidationError as exc:
        return _refuse_400(_validation_code(exc), str(exc))
    try:
        cleaned = crew_log_emit.safe_work_fields(cleaned)
    except crew_log_emit.WorkFieldError as exc:
        return _refuse_400(work_ledger.CODE_INVALID_VALUE, str(exc))

    # The record is refused BEFORE the cache commits: the widest entry this report
    # can produce (the committed `pr` may be an earlier report's) must fit a line.
    # It is built the way the entry is -- over the baseline an unstamped item is
    # owed, with every store-generated field at its widest -- so a write the probe
    # passes cannot then commit and be undone with a 503 where a 400 was promised.
    probe_item, probe_board = await asyncio.to_thread(
        _current_item_and_board, conductor_key, item_id
    )
    probe, invalid = _entry_probe_with_baseline(
        conductor_key,
        probe_item,
        probe_board,
        actor="worker",
        by=caller,
        action="report",
        item_id=item_id,
        generation=_WIDEST_HEX_ID,
        round=_WIDEST_COUNTER,
        status=cleaned.get("status"),
        summary=cleaned.get("summary"),
        artifacts=cleaned.get("artifacts") or {},
        pr=work_ledger.MAX_PR,
        last_report_at=_WIDEST_STAMP,
        event=_WIDEST_EVENT_TEXT,
        event_kind="report",
        event_id=_WIDEST_HEX_ID,
        event_ts=_WIDEST_STAMP,
    )
    if invalid is not None:
        return invalid
    assert probe is not None
    if not crew_log_emit.work_entry_fits(probe):
        if _baseline_overflows(
            conductor_key,
            probe_item,
            probe_board,
            skip=_WORKER_REPORT_FIELDS,
            actor="worker",
            by=caller,
            action="report",
            item_id=item_id,
            generation=_WIDEST_HEX_ID,
            round=_WIDEST_COUNTER,
            pr=work_ledger.MAX_PR,
            last_report_at=_WIDEST_STAMP,
            event=_WIDEST_EVENT_TEXT,
            event_kind="report",
            event_id=_WIDEST_HEX_ID,
            event_ts=_WIDEST_STAMP,
        ):
            return _refuse_item_too_large(key, "work_report", item_id)
        _audit(key, "work_report", "denied", resources=item_id, error="work_entry_too_large")
        return _refuse_400(
            "work_entry_too_large",
            "the report's crew-log record does not fit one log line; shorten it",
        )

    async def _under_board() -> web.Response:
        # Re-checked under the lock: a write ahead of this one may have marked the
        # cache dirty (its undo failed) after this one's early check passed, and a
        # commit over that would build on the damage.
        dirty = await _refuse_if_dirty(conductor_key, caller, "work_report")
        if dirty is not None:
            return dirty
        snapshot = await asyncio.to_thread(
            functools.partial(work_ledger.snapshot_for_write, conductor_key, item_id=item_id)
        )

        async def _commit_and_record() -> web.Response:
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
                _audit(caller, "work_report", "denied", resources=item_id, error=exc.code)
                return _refuse_store_error(exc)
            except OSError:
                logger.warning("work ledger report failed for %s", item_id, exc_info=True)
                return _refuse_503("ledger_write_failed", "ledger write failed; try again")

            item = result["item"]
            board = await asyncio.to_thread(work_ledger.read_conductor, conductor_key)
            established, board = await _board_header(conductor_key, board)
            if not established:
                return await _refuse_unrecorded(
                    caller, "work_report", item.item_id, conductor_key, snapshot
                )
            # The entry carries what the COMMIT holds, not what the request said: an
            # omitted `artifacts` clears the map, and the fold reads an absent field as
            # unchanged, so only the committed value keeps the rebuild equal to the cache.
            landed = await asyncio.to_thread(
                crew_log_emit.on_work_recorded,
                acting_unit,
                _entry_with_baseline(
                    conductor_key,
                    item,
                    board,
                    actor="worker",
                    by=caller,
                    action="report",
                    item_id=item.item_id,
                    generation=getattr(board, "generation", None) or None,
                    round=item.round,
                    status=item.status,
                    summary=item.summary,
                    artifacts=item.artifacts,
                    pr=item.pr,
                    last_report_at=item.last_report_at,
                    event=getattr(result.get("event"), "text", None),
                    event_kind="report",
                    event_id=getattr(result.get("event"), "id", None) or None,
                    event_ts=getattr(result.get("event"), "ts", None) or None,
                ),
            )
            if not landed:
                return await _refuse_unrecorded(
                    caller, "work_report", item.item_id, conductor_key, snapshot
                )
            await asyncio.to_thread(_mark_recorded, conductor_key, item.item_id)
            _audit(caller, "work_report", "ok", resources=f"{item_id} status={item.status}")
            return web.json_response(
                {"ok": True, "item_id": item.item_id, "status": item.status, "round": item.round}
            )

        # Shielding alone detaches this transaction when the handler is cancelled.
        # Drain it instead: after the cache commit, either the crew-log append lands
        # or the snapshot is restored before the cancellation is re-raised.
        return await _drain_before_cancelling(_commit_and_record())

    async with _board_lock(conductor_key):
        return await _under_board()


# The widest values the store can commit for the fields it generates, so a fit
# probe built before the commit is never narrower than the entry written after it.
# Event text is capped in characters; an astral character serializes as twelve bytes.
_WIDEST_COUNTER = 2**31 - 1
_WIDEST_ITEM_ID = "it_ffffffff"
_WIDEST_EVENT_TEXT = "W" * (12 * work_ledger.MAX_EVENT_TEXT_CHARS)
# Stamps are ``_now_iso()`` (seconds precision, local offset, at most an
# ``+HH:MM:SS`` offset); a generation is ``token_hex(8)`` and an event id a
# sha256 prefix, both 16 hex characters.
_WIDEST_STAMP = "9999-12-31T23:59:59+23:59:59"
_WIDEST_HEX_ID = "f" * 16


def _current_item_and_board(slot: str, item_id: str | None) -> tuple[Any, Any]:
    """The cached item and header as they stand, ``None`` for each that is absent.

    Read for the fit probe only: a baseline is due when the item is unstamped and
    the probe must carry it, or a near-limit write would commit and be undone
    with a 503 where a 400 was promised. Nothing here bootstraps; a missing
    ledger, or an id the store will refuse, reads as ``None``.
    """
    try:
        header = work_ledger.read_conductor(slot)
        item = work_ledger.read_work_item(slot, item_id) if item_id else None
    except (WorkLedgerError, OSError):
        return None, None
    return item, header


def _mark_goal_recorded(slot: str) -> None:
    """Stamp the header as held by the log; a failure only defers the header's check."""
    try:
        work_ledger.mark_goal_recorded(slot)
    except (WorkLedgerError, OSError):
        logger.debug("work ledger: could not mark the header recorded", exc_info=True)


def _mark_recorded(slot: str, item_id: str) -> None:
    """Stamp the item as held whole by the log; a failure only costs one more baseline."""
    try:
        work_ledger.mark_item_recorded(slot, item_id)
    except (WorkLedgerError, OSError):
        logger.debug("work ledger: could not mark %s recorded", item_id, exc_info=True)


def _entry_with_baseline(slot: str, item: Any, header: Any, **fields: Any) -> dict[str, Any]:
    """A ``work/recorded`` payload: the action's fields over a baseline when one is due.

    A field the action passes as ``None`` means "this action did not set it", not
    "clear it": it must not knock the baseline's value out, or a legacy item whose
    first recorded mutation is a ``decide`` would be recorded without its title.
    """
    baseline = _baseline_fields(item, header) if item is not None else {}
    set_fields = {name: value for name, value in fields.items() if value is not None}
    return _work_entry(slot, **crew_log_emit.safe_work_fields({**baseline, **set_fields}))


def _entry_probe_with_baseline(
    slot: str, item: Any, header: Any, **fields: Any
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Build a fit probe, mapping legacy redaction collisions to HTTP 400."""
    try:
        return _entry_with_baseline(slot, item, header, **fields), None
    except crew_log_emit.WorkFieldError as exc:
        return None, _refuse_400(work_ledger.CODE_INVALID_VALUE, str(exc))


#: The fields a worker's report sets; removed from a baseline to ask whether the
#: rest -- what the worker cannot change -- is what fails to fit.
_WORKER_REPORT_FIELDS: tuple[str, ...] = (
    "status",
    "summary",
    "artifacts",
    "pr",
    "round",
    "last_report_at",
)


def _baseline_overflows(
    slot: str, item: Any, header: Any, *, skip: tuple[str, ...], **fields: Any
) -> bool:
    """Whether the committed item itself, not the request, is what cannot fit.

    An entry about an item the record has never held whole carries the whole item
    (:func:`_baseline_fields`). When such an entry does not fit one log line, two
    things can be too big: the request's own text, which its sender can shorten,
    or the committed item, which only its conductor can shrink. Telling the two
    apart is the difference between a refusal that helps and one that strands the
    worker: with the fields this write sets removed from the baseline, a probe
    that still does not fit is the item's own size. The acceptance is the only
    field without a cap of its own, so it is what overflows in practice.
    """
    if item is None or not _baseline_fields(item, header):
        return False
    base = _baseline_fields(item, header)
    for name in skip:
        base.pop(name, None)
    return not crew_log_emit.work_entry_fits(_work_entry(slot, **{**base, **fields}))


def _refuse_item_too_large(key: str, operation: str, item_id: str) -> web.Response:
    _audit(key, operation, "denied", resources=item_id, error="work_item_too_large")
    return _refuse_400(
        "work_item_too_large",
        f"item {item_id} predates the record, so its first recorded write must carry "
        "the whole item, and the committed item does not fit one crew-log line (its "
        "acceptance is the only field without a cap); no shorter write about it can "
        f"land. The conductor shrinks it first: work_ledger_record action=accept "
        f"item_id={item_id} with a smaller acceptance records the item whole, and "
        "writes about it land from then on.",
        field="acceptance",
    )


def _baseline_fields(item: Any, header: Any) -> dict[str, Any]:
    """The whole committed item, for an entry about an item the record never held.

    An item from before the projection (or whose recorded create was undone) has
    no create entry, so a delta about it could never rebuild it. Until the log has
    held it whole once (`recorded_at`), every entry about it carries all of it.
    """
    if getattr(item, "recorded_at", ""):
        return {}
    return {
        "baseline": True,
        # The board's own lineage and goal ride along: a nested board whose
        # header the record never saw would otherwise rebuild as a top-level one.
        "depth": getattr(header, "depth", 0),
        "parent_item": getattr(header, "parent_item", None) or None,
        "goal": getattr(header, "goal", "") or None,
        "board_round": getattr(header, "round", 0),
        "board_created_at": getattr(header, "created_at", "") or None,
        "title": item.title,
        "acceptance": item.acceptance,
        "state": item.state,
        "verdict": item.verdict,
        "decision": item.decision or None,
        "worker_session_key": item.worker_session_key or None,
        "round": item.round,
        "fails": item.fails,
        "status": item.status,
        "summary": item.summary or None,
        "artifacts": item.artifacts,
        "pr": item.pr,
        "created_at": item.created_at or None,
        "last_report_at": item.last_report_at or None,
        "closed_at": item.closed_at or None,
    }


#: One asyncio lock per board, held across a write's commit and its crew-log
#: append and across a rebuild, so a rebuild can never fold between the two and
#: write the cache back to before a mutation the log then records. Every writer
#: and every rebuild runs on this gateway's one loop, which is what makes an
#: in-process lock the complete answer.
_BOARD_LOCKS: dict[str, asyncio.Lock] = {}


def _board_lock(slot: str) -> asyncio.Lock:
    lock = _BOARD_LOCKS.get(slot)
    if lock is None:
        lock = _BOARD_LOCKS[slot] = asyncio.Lock()
    return lock


async def _drain_before_cancelling(coro: Any) -> Any:
    """Finish *coro* before propagating this caller's cancellation.

    Repeated shutdown cancellation is absorbed only while the task drains. The
    transaction is bounded by the crew-log append timeout and finite cache writes,
    so this cannot keep shutdown alive indefinitely.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            cancelled = True
            if not task.done():
                continue
            raise


async def _refuse_if_dirty(slot: str, key: str, operation: str) -> web.Response | None:
    """409 while the board's cache is flagged dirty: only a rebuild may touch it.

    The flag is set when an undo failed, so the cache may hold a mutation the
    record never saw or half of a rebuild. Serving or writing over that would
    pass the damage on; the refusal names the cure.
    """
    reason = await asyncio.to_thread(work_ledger.cache_dirty, slot)
    if reason is None:
        return None
    _audit(key, operation, "denied", error=work_ledger.CODE_CACHE_DIRTY)
    return _refuse_409(
        work_ledger.CODE_CACHE_DIRTY,
        f"the board's cache is flagged dirty ({reason}); run work_ledger_rebuild to "
        "rebuild it from the record, or, to keep the cache as it stands, remove the "
        f"{work_ledger.DIRTY_FILE!r} marker in the board's work-ledger directory",
    )


async def _board_header(slot: str, header: Any) -> "tuple[bool, Any]":
    """*slot*'s conductor record for an entry, and whether it could be established.

    The generation an entry carries comes off this record, and a MISSING one is two
    different facts: a board minted before the stamp existed carries none and folds
    by its own generationless entries, while a header read that faulted also carries
    none. They cannot be treated alike here, because the fold omits a generationless
    entry once a stamped board is live -- so appending one on a fault loses a write
    this route has already committed, and the next rebuild is where it disappears.

    A header the commit handed back is the board's own and is taken as it is,
    generation or not. Only an ABSENT one is re-read, strictly: that is the contract
    the header writers use for the same reason, raising on an I/O fault and
    answering None only for a ledger that is really gone. Either of those leaves the
    generation unestablished, and the caller refuses the write rather than appending
    an entry the fold would drop. The record also carries the baseline's board
    fields, so resolving it once serves both.
    """
    if header is not None:
        return True, header
    try:
        record = await asyncio.to_thread(
            functools.partial(work_ledger.read_conductor, slot, strict=True)
        )
    except OSError:
        logger.warning("could not read the conductor record for %s", slot, exc_info=True)
        return False, None
    return record is not None, record


async def _refuse_unrecorded(
    key: str,
    operation: str,
    item_id: str | None,
    slot: str,
    snapshot: dict[str, bytes | None],
    *,
    created: str | None = None,
    worker_session_key: str | None = None,
) -> web.Response:
    """The store committed but the crew log did not confirm the entry.

    The log is the record, so the cache must not keep a mutation the log never
    saw: every file the write touched is put back from the *snapshot* the route
    took before the write, and an item the write created is removed. That undo
    needs no fold, so it is exact for a board from before the projection as much
    as for a fresh one; it runs off the loop, and it takes the store's own file
    locks for the files it puts back -- the board lock this caller holds is an
    in-process one, so it does not exclude a second gateway on the same store.
    When even that fails the refusal stands and a rebuild converges.
    Answered as a failure so the caller never believes the mutation is recorded;
    the committed `item_id` is returned so a `create` is not repeated on retry.
    """
    logger.error("work ledger %s for %s committed without a crew-log record", operation, key)
    _audit(key, operation, "error", resources=item_id or "", error="crew_log_unrecorded")
    # What this write left in those files, read before the undo touches them: the
    # undo puts back only a file still holding it, so a write a second gateway
    # committed in between survives and flags the cache instead of being replaced.
    after = await asyncio.to_thread(work_ledger.current_bytes, snapshot)
    try:
        await asyncio.to_thread(
            functools.partial(
                work_ledger.restore_snapshot,
                slot,
                snapshot,
                created_item=created,
                item_id=item_id,
                worker_session_key=worker_session_key,
                expected=after,
            )
        )
    except Exception:  # noqa: BLE001 - the refusal is the answer either way
        logger.warning("could not undo the unrecorded write on %s", slot, exc_info=True)
        try:
            await asyncio.to_thread(
                work_ledger.mark_cache_dirty, slot, "an unrecorded write could not be undone"
            )
        except Exception:  # noqa: BLE001 - nothing left to do but say so
            logger.error("could not flag the cache dirty for %s", slot, exc_info=True)
    return web.json_response(
        {
            "ok": False,
            "error": "the write was stored but its crew-log record was not confirmed",
            "code": "crew_log_unrecorded",
            "item_id": item_id,
        },
        status=503,
    )


def _work_entry(slot: str, **fields: Any) -> dict[str, Any]:
    """One ``work/recorded`` payload: the board's slot plus the fields this write set.

    ``None`` values are dropped, because an omitted field means "unchanged" to
    every reader of the entry, and the type's own validation would refuse a null.
    """
    payload: dict[str, Any] = {"slot": slot}
    payload.update({name: value for name, value in fields.items() if value is not None})
    return payload


def _acting_unit(
    state: DashboardState, key: str, operation: str, resources: str
) -> tuple[str | None, web.Response | None]:
    """The crew-log unit *key* is serving, or the refusal that stops the write.

    The work ledger is a projection of the crew log: every write appends one
    ``work/recorded`` entry to the acting session's log, and the store files are
    only a cache rebuilt from those entries. A write with nowhere to append is
    therefore refused up front -- with the emitter off, or with a caller whose
    unit the registry cannot name -- rather than leaving the cache holding a
    mutation the log never saw.
    """
    if not crew_log_emit.enabled():
        _audit(key, operation, "denied", resources=resources, error="crew_log_off")
        return None, _refuse_409(
            "crew_log_off",
            "the crew log is off and the work ledger records only into it; "
            f"start the gateway with {crew_log_emit.CREW_LOG_ENV}=1",
        )
    unit = unit_for_session_key(getattr(state, "sessions", None), key)
    if not unit or unit == UNKNOWN:
        _audit(key, operation, "denied", resources=resources, error="crew_log_unit_unknown")
        return None, _refuse_409(
            "crew_log_unit_unknown",
            "this session's crew log unit is not known; there is nothing to record into",
        )
    return unit, None


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
    ``orphaned`` / ``stale`` / ``acceptance_concrete`` flags, each item's newest
    events, and a ready-to-pipe ``accept_batch`` built from ``acceptance`` ALONE —
    never from a worker's claimed ``pr``, which is surfaced beside the item instead.

    ``accept_batch`` holds only the items whose bar is concrete, so an item still
    carrying a ``"TBD"`` pull request number is absent from it; ``acceptance_concrete``
    on the item row is why. Each entry carries the item's ``status`` so the conductor
    can apply its own "``done`` only" filter without a second lookup — the filter stays
    the conductor's to apply.
    """
    key, refusal = await _caller_key(request, "work_ledger_read")
    if refusal is not None:
        return refusal
    assert key is not None
    # Under the board lock for the same reason the brief read is: a write's
    # cache commit and its record append are two steps, and a read between them
    # would publish a mutation the record may yet roll back. The header and the
    # items are read under one hold, so the response is one snapshot.
    async with _board_lock(key):
        record, lrefusal = await _own_ledger(key, "work_ledger_read")
        if lrefusal is not None:
            return lrefusal
        dirty = await _refuse_if_dirty(key, key, "work_ledger_read")
        if dirty is not None:
            return dirty
        items = await asyncio.to_thread(work_ledger.list_work_items, key)
        event_tails: dict[str, list[work_ledger.WorkEvent]] = {}
        for item in items:
            event_tails[item.item_id] = await asyncio.to_thread(
                _tail_events, key, item.item_id, _MAX_EVENT_TAIL
            )
    assert record is not None

    state: DashboardState = request.app["state"]
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
        # Why an item is (or is not) in ``accept_batch``, on the item itself. Without
        # it a conductor sees an item it dispatched simply missing from the batch and
        # has no way to tell "bar not filled in yet" from "the read dropped it".
        row["acceptance_concrete"] = work_ledger.is_acceptance_concrete(item.acceptance)
        events = event_tails[item.item_id]
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

    ``stale`` is the conjunction "quiet past the window AND not running AND the
    worker's last word still left the move with it", and the running half means the
    worker is doing something (a thirty-minute build), not that its session exists. An idle worker whose tab is still open but which
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
    try:
        cleaned = crew_log_emit.safe_work_fields(cleaned)
    except crew_log_emit.WorkFieldError as exc:
        return _refuse_400(work_ledger.CODE_INVALID_VALUE, str(exc))

    action = str(cleaned.get("action") or "")
    if action not in RECORD_ACTIONS:
        return _refuse_400(
            work_ledger.CODE_INVALID_ACTION,
            f"unknown action {action!r}; expected one of: {', '.join(sorted(RECORD_ACTIONS))}",
        )

    state: DashboardState = request.app["state"]
    unit, urefusal = _acting_unit(state, key, "work_ledger_record", action)
    if urefusal is not None:
        return urefusal
    dirty = await _refuse_if_dirty(key, key, "work_ledger_record")
    if dirty is not None:
        return dirty
    assert unit is not None

    if action == "bind":
        refusal = _refuse_unowned_worker(request, key, cleaned.get("worker_session_key"))
        if refusal is not None:
            return refusal

    # Refused BEFORE the ledger is even bootstrapped: the widest entry this action
    # can produce,
    # with the store-generated fields at their widest, must fit one log line. The
    # request's acceptance IS the committed one (the store keeps it verbatim).
    probe_item, probe_header = await asyncio.to_thread(
        _current_item_and_board, key, cleaned.get("item_id")
    )
    # ``close`` logs the item's decision whether or not this request set one, so
    # the probe carries the committed value when the request carries none.
    probe_decision = cleaned.get("decision")
    if probe_decision is None and "decision" in WORK_CONDUCTOR_FIELDS.get(action, ()):
        probe_decision = getattr(probe_item, "decision", None)
    probe, invalid = _entry_probe_with_baseline(
        key,
        probe_item if action != "create" else None,
        probe_header,
        actor="conductor",
        by=key,
        action=action,
        item_id=cleaned.get("item_id") or _WIDEST_ITEM_ID,
        generation=_WIDEST_HEX_ID,
        goal=cleaned.get("goal"),
        round=_WIDEST_COUNTER,
        depth=work_ledger.MAX_DEPTH,
        parent_item=_WIDEST_ITEM_ID,
        title=cleaned.get("title"),
        acceptance=cleaned.get("acceptance"),
        worker_session_key=cleaned.get("worker_session_key"),
        decision=probe_decision,
        verdict=cleaned.get("verdict"),
        state="abandoned",
        fails=_WIDEST_COUNTER,
        event=_WIDEST_EVENT_TEXT,
        event_kind="decision",
        event_id=_WIDEST_HEX_ID,
        event_ts=_WIDEST_STAMP,
        created_at=_WIDEST_STAMP,
        closed_at=_WIDEST_STAMP,
    )
    if invalid is not None:
        return invalid
    assert probe is not None
    if not crew_log_emit.work_entry_fits(probe):
        if action != "create" and _baseline_overflows(
            key,
            probe_item,
            probe_header,
            skip=WORK_CONDUCTOR_FIELDS.get(action, ()),
            actor="conductor",
            by=key,
            action=action,
            item_id=cleaned.get("item_id") or _WIDEST_ITEM_ID,
            generation=_WIDEST_HEX_ID,
            round=_WIDEST_COUNTER,
            depth=work_ledger.MAX_DEPTH,
            parent_item=_WIDEST_ITEM_ID,
            fails=_WIDEST_COUNTER,
            event=_WIDEST_EVENT_TEXT,
            event_kind="decision",
            event_id=_WIDEST_HEX_ID,
            event_ts=_WIDEST_STAMP,
            created_at=_WIDEST_STAMP,
            closed_at=_WIDEST_STAMP,
        ):
            return _refuse_item_too_large(key, "work_ledger_record", str(cleaned.get("item_id")))
        _audit(key, "work_ledger_record", "denied", resources=action, error="work_entry_too_large")
        return _refuse_400(
            "work_entry_too_large",
            "the action's crew-log record does not fit one log line; shorten it",
        )

    async def _under_board() -> web.Response:
        # Re-checked under the lock, for the same reason the report route does.
        dirty = await _refuse_if_dirty(key, key, "work_ledger_record")
        if dirty is not None:
            return dirty
        # The worker key is folded through ``session_ledger.ledger_key`` before the
        # store writes the binding (see the module note); the snapshot names the
        # same path, or a failed bind's binding would escape the undo.
        raw_worker = cleaned.get("worker_session_key")
        folded_worker = (
            session_ledger.ledger_key(raw_worker)
            if isinstance(raw_worker, str) and raw_worker
            else None
        )
        snapshot = await asyncio.to_thread(
            functools.partial(
                work_ledger.snapshot_for_write,
                key,
                item_id=cleaned.get("item_id"),
                worker_session_key=folded_worker,
            )
        )
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
        header = result.get("conductor")
        event = result.get("event")
        established, header = await _board_header(key, header)
        if not established:
            return await _refuse_unrecorded(
                key,
                "work_ledger_record",
                getattr(item, "item_id", None),
                key,
                snapshot,
                created=getattr(item, "item_id", None) if action == "create" else None,
                worker_session_key=folded_worker,
            )
        holder = item if item is not None else header
        # Each field comes from the COMMITTED record (item or header), never from the
        # request, so the fold rebuilds what the cache holds; each action logs the
        # fields it sets -- the same table the fold applies (`WORK_CONDUCTOR_FIELDS`).
        sets = WORK_CONDUCTOR_FIELDS.get(action, ())
        landed = await asyncio.to_thread(
            crew_log_emit.on_work_recorded,
            unit,
            _entry_with_baseline(
                key,
                item if action != "create" else None,
                header,
                actor="conductor",
                by=key,
                action=action,
                item_id=getattr(item, "item_id", None),
                generation=getattr(header, "generation", None) or None,
                goal=getattr(header, "goal", None) if action == "goal" else None,
                round=(
                    getattr(holder, "round", None) if action == "goal" or "round" in sets else None
                ),
                goal_version=getattr(header, "goal_version", None) if action == "goal" else None,
                # Lineage is board identity, not an action's delta: every conductor
                # entry carries it, so a board whose goal was never recorded, or
                # whose header is lost, still rebuilds at its depth.
                depth=getattr(header, "depth", None),
                parent_item=getattr(header, "parent_item", None),
                title=getattr(item, "title", None) if "title" in sets else None,
                acceptance=getattr(item, "acceptance", None) if "acceptance" in sets else None,
                worker_session_key=(
                    getattr(item, "worker_session_key", None)
                    if "worker_session_key" in sets
                    else None
                ),
                decision=getattr(item, "decision", None) if "decision" in sets else None,
                verdict=getattr(item, "verdict", None) if "verdict" in sets else None,
                state=getattr(item, "state", None) if "state" in sets else None,
                fails=getattr(item, "fails", None) if "fails" in sets else None,
                event=getattr(event, "text", None),
                event_kind=getattr(event, "kind", None),
                event_id=getattr(event, "id", None) or None,
                event_ts=getattr(event, "ts", None) or None,
                created_at=getattr(item, "created_at", None) if action == "create" else None,
                closed_at=getattr(item, "closed_at", None) if action == "close" else None,
            ),
        )
        if not landed:
            return await _refuse_unrecorded(
                key,
                "work_ledger_record",
                getattr(item, "item_id", None),
                key,
                snapshot,
                created=getattr(item, "item_id", None) if action == "create" else None,
                worker_session_key=folded_worker,
            )
        if item is not None:
            await asyncio.to_thread(_mark_recorded, key, item.item_id)
        if action == "goal":
            await asyncio.to_thread(_mark_goal_recorded, key)
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

    # Shielding alone detaches this transaction when the handler is cancelled.
    # Drain it instead, as the report route does: the cache commit and the
    # crew-log append both finish -- or the snapshot is restored -- before the
    # cancellation is re-raised and the board lock is released. Without this a
    # cancelled request leaves a committed cache write the log never saw, and
    # the lock goes back with that divergence visible to the next reader.
    async with _board_lock(key):
        return await _drain_before_cancelling(_under_board())


async def api_work_ledger_rebuild(request: web.Request) -> web.Response:
    """POST /api/work-ledger/rebuild -- re-materialise this conductor's board.

    The cache under ``work-ledger/<conductor>/`` is rewritten from the crew log's
    ``work`` fold over this session's own slot, so a board whose files were lost
    or damaged comes back from the record. The caller can only rebuild its own
    board: the slot is the caller's key, never a parameter.
    """
    key, refusal = await _caller_key(request, "work_ledger_rebuild")
    if refusal is not None:
        return refusal
    assert key is not None
    if not crew_log_emit.enabled():
        _audit(key, "work_ledger_rebuild", "denied", error="crew_log_off")
        return _refuse_409(
            "crew_log_off",
            "the crew log is off, so there is no record to rebuild from; "
            f"start the gateway with {crew_log_emit.CREW_LOG_ENV}=1",
        )
    try:
        async with _board_lock(key):
            # Drained for the same reason as a write: a rebuild re-materialises
            # every file, so a cancellation that released the board lock mid-way
            # would publish a board half old and half rebuilt.
            counts = await _drain_before_cancelling(
                asyncio.to_thread(work_ledger.rebuild_from_projection, key)
            )
    except CrewLogError as exc:
        _audit(key, "work_ledger_rebuild", "denied", error="crew_log_unreadable")
        return _refuse_409("crew_log_unreadable", f"the crew log could not be read: {exc}")
    except WorkLedgerError as exc:
        _audit(key, "work_ledger_rebuild", "denied", error=exc.code)
        return _refuse_store_error(exc)
    except OSError:
        logger.warning("work ledger rebuild failed for %s", key, exc_info=True)
        return _refuse_503("ledger_write_failed", "ledger write failed; try again")
    _audit(key, "work_ledger_rebuild", "ok", resources=f"items={counts['items']}")
    return web.json_response({"ok": True, **counts})


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

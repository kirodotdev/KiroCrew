"""The coded answers every work-ledger route refuses with, and the request plumbing.

The status each store code maps to, the codes this layer owns, the literal per-status
sinks the error-code contract reads, the SEL row a ledger touch leaves, and the JSON
body and schema-refusal helpers the write routes share.

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew import work_ledger
from kiro_crew.validation import ValidationError
from kiro_crew.work_ledger import WorkLedgerError

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")


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
    work_ledger.CODE_GOAL_CONCURRENCY_CAP_REACHED: 409,
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

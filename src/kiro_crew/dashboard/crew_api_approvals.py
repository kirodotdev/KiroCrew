"""The host owner's tool approvals without a dashboard: list them, read one, answer one.

Handlers for ``GET /v1/crew/owner/approvals``, ``GET /v1/crew/owner/approvals/{approval_id}``
and ``POST /v1/crew/owner/approvals/{approval_id}/decision``. They are served only through
:func:`kiro_crew.dashboard.crew_api_owner.owner_route` and additionally require the host
owner: a Shared Crew teammate, an app token and a loopback internal caller get 403.

The gateway keeps an approval in memory, in one of two places, under the id its caller
chose, which can recur:

* ``coordinator``: a background approval (a cron job, a subagent, a task run) in
  ``state._pending_approvals``, named by the record's ``instance``.
* ``native``: a chat turn's own tool prompt in its slot's ``_approval_futures``, named by
  the ``mid`` of the permission row the slot registered it with.

:class:`ApprovalIndex` gives each such request one ``apr_`` id, the first time an Owner
route sees it, and keeps it in memory for the life of the gateway process. An answer
goes to the request the id was given to, through the resolvers the dashboard uses,
and never to a later request under the same caller id. Nothing in this index is
written to disk. After a restart an old ``apr_`` id is unknown and returns 404.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.crew_api_grant_requests import _host_owner_refusal

logger = logging.getLogger(__name__)

NAMESPACE = "default"

# shortcut: proposals to be confirmed (the Crew API RFC, "Routes outside the 59"). The
# record keeps this many approvals; past it the oldest answered or expired ones are
# dropped, never a pending one.
MAX_RECORDS = 1000
#: The longest tool name, reason and argument text a record keeps.
TOOL_MAX = 200
REASON_MAX = 500
ARGUMENTS_MAX = 2000
LIMIT_DEFAULT = 20
LIMIT_MAX = 100
_STATES = ("pending", "allowed", "denied", "expired")
_BODY_MAX = 1024

#: Where a request lives: ``(origin, slot key, instance)``.
Target = tuple[str, str, str]


@dataclass(frozen=True)
class LiveApproval:
    """One request that is waiting now, as the gateway holds it."""

    target: Target
    request_id: str
    future: Any
    tool: str
    reason: str
    arguments: str
    client_id: str | None


def _cut(value: object, limit: int) -> str:
    return str(value or "")[:limit]


def _app_of(slot: Any) -> str | None:
    app = getattr(slot, "_app", "")
    return app if isinstance(app, str) and app else None


def live_approvals(state: Any) -> list[LiveApproval]:
    """The requests waiting now. Run on the event loop: it reads live registries.

    The text of every field was redacted when the gateway stored it. An MCP
    elicitation shares a slot's registry but is the server's question, not a tool
    approval, so it is left out; so is a native request with no permission row to
    name it by.
    """
    # Imported here: chat_handlers imports the handlers package at module scope.
    from kiro_crew.dashboard.chat_handlers import _pending_elicitation_ids
    from kiro_crew.dashboard.state import row_mid

    live: list[LiveApproval] = []
    slots = dict(state._slots)
    for request_id, record in list(state._pending_approvals.items()):
        future = state._approval_futures.get(request_id)
        instance = record.get("instance")
        if future is None or future.done() or not isinstance(instance, str) or not instance:
            continue
        slot_key = str(record.get("slot") or "")
        live.append(
            LiveApproval(
                target=("coordinator", slot_key, instance),
                request_id=request_id,
                future=future,
                tool=_cut(record.get("tool"), TOOL_MAX),
                reason=_cut(record.get("tool_purpose"), REASON_MAX),
                arguments=_cut(record.get("tool_input"), ARGUMENTS_MAX),
                client_id=_app_of(slots.get(slot_key)),
            )
        )
    for slot in slots.values():
        waiting = [(rid, f) for rid, f in slot._approval_futures.items() if not f.done()]
        if not waiting:
            # Nothing waits here, so no row is read: reading a restored session's
            # rows loads its transcript from disk on the event loop.
            continue
        elicitations = _pending_elicitation_ids(slot)
        for request_id, future in waiting:
            mid = slot.approval_instance(request_id)
            if request_id in elicitations or not mid:
                continue
            meta: dict[str, Any] = {}
            for row in reversed(slot.messages):
                if row_mid(row) == mid:
                    try:
                        parsed = json.loads(row.get("cls") or "")
                    except (TypeError, ValueError):
                        parsed = None
                    meta = parsed if isinstance(parsed, dict) else {}
                    break
            live.append(
                LiveApproval(
                    target=("native", slot.key, mid),
                    request_id=request_id,
                    future=future,
                    tool=_cut(meta.get("tool_name") or meta.get("tool_title"), TOOL_MAX),
                    reason=_cut(meta.get("tool_purpose"), REASON_MAX),
                    arguments=_cut(meta.get("tool_input"), ARGUMENTS_MAX),
                    client_id=_app_of(slot),
                )
            )
    return live


def _ending(future: Any) -> tuple[str, str | None]:
    """The state and expiry reason of a request that ended outside these routes.

    The future says whether the tool ran, not who decided: the dashboard's answer
    and a Stop both settle it as a rejection. An unanswered wait cancels it, and so
    does a turn cancelled under it; both are reported as ``timeout``.
    """
    if future.cancelled() or not future.done() or future.exception() is not None:
        return "expired", "timeout"
    result = future.result()
    if result is True or (isinstance(result, str) and not result.startswith("rejected")):
        return "allowed", None
    return "denied", None


class ApprovalIndex:
    """The approvals seen by Owner routes during this gateway process. Event-loop only."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._records: dict[str, dict[str, Any]] = {}
        #: The futures of the requests waiting in this process, read when one ends.
        self._watched: dict[Target, Any] = {}

    def observe(self, live: list[LiveApproval]) -> dict[Target, tuple[str, str | None]]:
        """Watch *live* and return how each watched request that left it ended.

        Run on the event loop, in the same step as :func:`live_approvals`.
        """
        now = {item.target for item in live}
        ended = {t: _ending(f) for t, f in self._watched.items() if t not in now}
        self._watched = {item.target: item.future for item in live}
        return ended

    def sync(
        self, live: list[LiveApproval], ended: dict[Target, tuple[str, str | None]]
    ) -> dict[str, dict[str, Any]]:
        """Bring the records up to *live* in one step on the event loop."""
        records = self._records
        now = int(self._clock())
        waiting = {item.target: item for item in live}
        known: set[Target] = set()
        for record in records.values():
            target: Target = (record["origin"], record["slot"], record["instance"])
            known.add(target)
            if record["state"] != "pending" or target in waiting:
                continue
            state, reason = ended.get(target, ("expired", "timeout"))
            record.update(state=state, expired_reason=reason, decided_at=now)
        for target, item in waiting.items():
            if target in known:
                continue
            approval_id = "apr_" + secrets.token_hex(16)
            records[approval_id] = {
                "id": approval_id,
                "namespace": NAMESPACE,
                "origin": target[0],
                "slot": target[1],
                "instance": target[2],
                "request_id": item.request_id,
                "tool": item.tool,
                "reason": item.reason,
                "arguments": item.arguments,
                "client_id": item.client_id,
                "state": "pending",
                "created_at": now,
                "decided_at": None,
                "expired_reason": None,
                "decided_by": None,
            }
        done = [key for key, record in records.items() if record["state"] != "pending"]
        for key in done[: max(0, len(records) - MAX_RECORDS)]:
            del records[key]
        return records

    def settle(self, approval_id: str, state: str, principal: str) -> dict[str, Any]:
        """Record the route's answer before yielding the event loop."""
        record = self._records[approval_id]
        record.update(
            state=state,
            decided_at=int(self._clock()),
            decided_by={"kind": "owner", "principal": principal, "via": "api"},
        )
        return record


def gateway_approval_index(state: Any) -> ApprovalIndex:
    """The gateway's own index, created on the event loop, one per *state*."""
    index = state.__dict__.get("_crew_api_approval_index")
    if index is None:
        index = state.__dict__["_crew_api_approval_index"] = ApprovalIndex()
    return index


def _approval(record: dict[str, Any]) -> dict[str, Any]:
    """A stored record in the contract's ``OwnerApproval`` shape."""
    return {
        "id": record["id"],
        "object": "approval",
        "namespace": record["namespace"],
        # The session the request was raised in; null for one with no session (a cron job's).
        "conversation": record["slot"] or None,
        "client_id": record["client_id"],
        "tool": record["tool"],
        "reason": record["reason"],
        "arguments": {"input": record["arguments"]},
        "state": record["state"],
        "created_at": record["created_at"],
        "decided_at": record["decided_at"],
        "expired_reason": record["expired_reason"],
        "decided_by": record["decided_by"],
    }


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _records(state: Any) -> tuple[ApprovalIndex, dict[str, dict[str, Any]]]:
    """Read live requests, observe their endings and sync without yielding the loop."""
    index = gateway_approval_index(state)
    live = live_approvals(state)
    return index, index.sync(live, index.observe(live))


async def list_approvals(request: web.Request) -> web.Response:
    """Approvals in one state (``pending`` when none is named), oldest first."""
    if (denied := await _host_owner_refusal(request, "crew_api.approvals.list")) is not None:
        return denied
    query = request.query
    wanted = query.get("state", "pending")
    raw_limit = query.get("limit", str(LIMIT_DEFAULT))
    if wanted not in _STATES:
        return _error(400, "invalid_request", "state must be pending, allowed, denied or expired")
    if len(raw_limit) > len(str(LIMIT_MAX)) or not (
        raw_limit.isascii() and raw_limit.isdigit() and 1 <= int(raw_limit) <= LIMIT_MAX
    ):
        return _error(400, "invalid_request", f"limit must be 1 to {LIMIT_MAX}")
    _, records = _records(request.app["state"])
    # The record is in the order the ids were given, which is oldest first.
    rows = [
        r
        for r in records.values()
        if r["state"] == wanted
        and ("conversation" not in query or r["slot"] == query["conversation"])
        and ("client_id" not in query or r["client_id"] == query["client_id"])
    ]
    if "after" in query:
        ids = [r["id"] for r in rows]
        if query["after"] not in ids:
            return _error(400, "invalid_request", "after names no approval in this list")
        rows = rows[ids.index(query["after"]) + 1 :]
    limit = int(raw_limit)
    data = [_approval(r) for r in rows[:limit]]
    return web.json_response(
        {
            "object": "list",
            "data": data,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
            "has_more": len(rows) > limit,
        }
    )


async def read_approval(request: web.Request) -> web.Response:
    """One approval by its ``apr_`` id, in whatever state it is."""
    if (denied := await _host_owner_refusal(request, "crew_api.approvals.read")) is not None:
        return denied
    _, records = _records(request.app["state"])
    record = records.get(request.match_info["approval_id"])
    if record is None:
        return _error(404, "not_found", "no such approval")
    return web.json_response(_approval(record))


def _answer(state: Any, request: web.Request, record: dict[str, Any], allow: bool) -> bool:
    """Answer the request *record* names, if it is still the one waiting.

    Run on the event loop with no await, so the request checked is the request
    answered. Both branches settle through the Shared Crew decision registry, as
    the dashboard's routes do, so the first answer wins.
    """
    from kiro_crew.shared_crew import decisions

    request_id, slot_key, instance = record["request_id"], record["slot"], record["instance"]
    if record["origin"] == "coordinator":
        pending = state._pending_approvals.get(request_id)
        future = state._approval_futures.get(request_id)
        # A settled future can still have its record until the waiting task wakes
        # and removes it; that request is answered and takes no second answer.
        if (
            not isinstance(pending, dict)
            or pending.get("instance") != instance
            or str(pending.get("slot") or "") != slot_key
            or future is None
            or future.done()
        ):
            return False
        reply = decisions.resolve_state_approval(
            state, request, request_id, allow, "approved" if allow else "rejected", ""
        )
    else:
        slot = state._slots.get(slot_key)
        future = slot._approval_futures.get(request_id) if slot is not None else None
        if (
            slot is None
            or future is None
            or future.done()
            or slot.approval_instance(request_id) != instance
        ):
            return False
        # ``deny`` refuses this one call and lets the turn go on.
        reply = decisions.resolve_pinned_slot_approval(
            state, request, slot, request_id, future, approved=allow, rejected_once=not allow
        )
    return reply is not None and reply[0] == 200


async def decide_approval(request: web.Request) -> web.Response:
    """Answer one pending approval with ``allow`` or ``deny``. The first answer wins."""
    if (denied := await _host_owner_refusal(request, "crew_api.approvals.decide")) is not None:
        return denied
    if request.content_length is None or request.content_length > _BODY_MAX:
        return _error(400, "invalid_request", "the body is missing its length or too large")
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or set(body) != {"decision"}:
        return _error(400, "invalid_request", 'the body must be {"decision": ...}')
    if body["decision"] not in ("allow", "deny"):
        return _error(400, "invalid_request", "decision must be allow or deny")
    allow = body["decision"] == "allow"
    state = request.app["state"]
    approval_id = request.match_info["approval_id"]
    index, records = _records(state)
    record = records.get(approval_id)
    if record is None:
        return _error(404, "not_found", "no such approval")
    refusal = None
    if record["state"] != "pending":
        # Answered, expired, or replaced under its caller id: an instance never waits
        # twice, so whatever waits under that caller id now is another request's.
        refusal = (409, "approval_already_resolved", "this approval is not pending")
    elif allow and record["origin"] == "native":
        from kiro_crew.dashboard.deny_override import is_deny_override_card

        slot = state._slots.get(record["slot"])
        future = slot._approval_futures.get(record["request_id"]) if slot is not None else None
        # Only a request still waiting has a card to check; any other answer is a 409
        # from _answer, and reading the rows of a restored session would load them.
        if (
            future is not None
            and not future.done()
            and is_deny_override_card(slot.messages, record["request_id"])
        ):
            # The slot's resolver refuses it too; this names why.
            refusal = (
                403,
                "approval_requires_dashboard",
                "a security-rule override is allowed from the dashboard only; deny works here",
            )
    if refusal is None and not _answer(state, request, record, allow):
        refusal = (409, "approval_already_resolved", "this approval is not pending")
    # Token auth set this subject; _host_owner_refusal proved it is the host owner's.
    principal = str(request["user"])
    outcome = "allowed" if allow and refusal is None else "denied"
    if refusal is None:
        record = index.settle(approval_id, outcome, principal)
    response = _error(*refusal) if refusal else web.json_response(_approval(record))

    def audit() -> None:
        from kiro_crew.sel import sel

        # The resolver writes the decision's own audit row; this one names the route.
        sel().log_api_access(
            caller=principal,
            operation="crew_api.approvals.decide",
            outcome=outcome,
            source="crew-api",
            resources=f"approval={approval_id}",
            error=refusal[1] if refusal else "",
        )

    try:
        await asyncio.to_thread(audit)
    except Exception:
        logger.warning("Owner approval API audit failed")
    return response

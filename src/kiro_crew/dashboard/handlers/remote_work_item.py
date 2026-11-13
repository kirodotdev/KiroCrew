"""The peer half of remote dispatch, wave 2 (rfc-conductor-remote-dispatch §2, §3).

Three pieces, all behind the ``work_ledger.remote_dispatch`` Feature Preview:

* :func:`work_item_for_create` and :func:`bind_work_item`, the two hooks
  ``POST /api/chat/slots`` calls: the first validates a create body's
  ``work_item`` before anything is minted, the second stores the hub's item in this
  crew's mailbox and binds the new slot to it before the slot is first broadcast.
* ``GET /api/chat/slots/{slot}/work-item``: the worker-owned fields of that slot's
  item plus its event tail, for the hub to ingest.
* ``PATCH /api/chat/slots/{slot}/work-item``: the hub's ``decision``, the one
  conductor-owned field that changes after create, and ``trust: false``, which
  ends the grant :func:`grant_remote_trust` armed at create.

With the preview off a create's ``work_item`` is ignored and both routes answer
404, so this gateway behaves exactly as it does without this module. Owner token
only, as the rest of ``api/chat`` the hub reaches through its proxy, and every
reply is redacted on the way out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from kiro_crew import remote_dispatch_mailbox as mailbox
from kiro_crew import work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard import remote_dispatch_headroom
from kiro_crew.dashboard.approval_posture import REMOTE_TRUST_SCOPE_PREFIX, remote_scope_denied
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.work_ledger import (
    _WIDEST_COUNTER,
    _WIDEST_EVENT_TEXT,
    _WIDEST_HEX_ID,
    _WIDEST_STAMP,
    _audit,
    _board_lock,
    _drain_before_cancelling,
    _entry_probe_with_baseline,
    _refuse_400,
    _refuse_404,
    _refuse_409,
    _refuse_503,
    _refuse_if_dirty,
    _refuse_store_error,
)
from kiro_crew.dashboard.handlers.work_ledger_board import _redact_deep

logger = logging.getLogger(__name__)

#: Events the GET returns when the hub names no ``limit``.
DEFAULT_EVENT_TAIL = 50

_CODE_NOT_FOUND = "work_item_not_found"
_CODE_NEEDS_NEW_SLOT = "work_item_needs_new_slot"


async def _preview_on() -> bool:
    """The ``work_ledger.remote_dispatch`` switch, read through the headroom gate's reader."""
    settings = await asyncio.to_thread(remote_dispatch_headroom.remote_dispatch_settings)
    return settings.enabled


def _not_found() -> web.Response:
    """The one 404 both routes give with the preview on: no such slot, or no hub item."""
    return _refuse_404(_CODE_NOT_FOUND, "no work item for this slot")


async def _redacted_json(doc: dict[str, Any]) -> web.Response:
    """*doc* through the Crew page's own recursive redactor, keys included.

    ``_redact_deep`` runs every key and string value through ``redact_via_context``,
    so both scanners run in the shared order.
    """
    return web.json_response(await asyncio.to_thread(_redact_deep, doc))


def _caller(request: web.Request) -> str:
    return str(request.get("user") or "unknown")


# ── the create hooks ──────────────────────────────────────────────────────


def _widest_report_fits(board: str, item: work_ledger.WorkItem) -> bool:
    """Whether the worker's widest report carrying all of *item* fits one log line.

    A mailbox item lives in no crew log until the worker reports, and a report on
    an unstamped item must carry the whole item (its baseline). An item that cannot
    fit there would refuse every report the worker makes, so a create or a decision
    that would produce one is refused instead. The probe is the report route's: the
    worker's fields at their caps.
    """
    header = work_ledger.ConductorRecord(slot_key=board, created_at=_WIDEST_STAMP)
    widest_artifacts = {
        f"{index:0{work_ledger.MAX_ARTIFACT_KEY_CHARS}d}": "W"
        * work_ledger.MAX_ARTIFACT_VALUE_CHARS
        for index in range(work_ledger.MAX_ARTIFACT_KEYS)
    }
    probe, invalid = _entry_probe_with_baseline(
        board,
        item,
        header,
        actor="worker",
        by="w" * mailbox.MAX_CONDUCTOR_KEY_CHARS,
        action="report",
        item_id=item.item_id,
        generation=_WIDEST_HEX_ID,
        round=_WIDEST_COUNTER,
        status="question",
        reason="needs_human",
        summary="W" * work_ledger.MAX_SUMMARY_CHARS,
        artifacts=widest_artifacts,
        pr=work_ledger.MAX_PR,
        last_report_at=_WIDEST_STAMP,
        event=_WIDEST_EVENT_TEXT,
        event_kind="report",
        event_id=_WIDEST_HEX_ID,
        event_ts=_WIDEST_STAMP,
        board_copy=True,
        mirror=True,
    )
    return invalid is None and probe is not None and crew_log_emit.work_entry_fits(probe)


async def work_item_for_create(
    request: web.Request, body: dict[str, Any]
) -> tuple[mailbox.MailboxRequest | None, web.Response | None]:
    """The create body's ``work_item``, validated, or the refusal for it.

    ``(None, None)`` when the body carries none or the preview is off: the create
    then runs exactly as it does today. Called before anything is minted, so a
    refusal builds nothing.
    """
    raw = body.get("work_item")
    if raw is None or not await _preview_on():
        return None, None
    denied = await require_owner_dashboard_request(request, "chat_slot_create.work_item")
    if denied is not None:
        return None, denied
    # A bound worker is a fresh session, never a reopened one, and never one that
    # carries an app or cron binding of its own.
    # A name is refused whatever its value, unless null or empty: the create turns
    # 0 or false into a string key that can name an open slot.
    for key in ("name", "app_agent_owner", "artifact", "ephemeral"):
        unset = body.get(key) in (None, "") or (key != "name" and body.get(key) is False)
        if not unset:
            return None, _refuse_400(
                _CODE_NEEDS_NEW_SLOT,
                f"a create with a work_item makes a new plain session; drop {key!r}",
                field=key,
            )
    if body.get("memory_mode", "persistent") != "persistent":
        # An incognito or temporary session is refused the worker tools, so it
        # could never read its brief or report.
        return None, _refuse_400(
            _CODE_NEEDS_NEW_SLOT,
            "a create with a work_item makes a persistent session; drop 'memory_mode'",
            field="memory_mode",
        )
    try:
        parsed = await asyncio.to_thread(mailbox.parse_work_item, raw)
    except work_ledger.WorkLedgerError as exc:
        _audit(_caller(request), "chat_slot_create.work_item", "denied", error=exc.code)
        return None, _refuse_store_error(exc)
    if await asyncio.to_thread(mailbox.item_exists, parsed):
        # Asked before the mint, so a retry builds nothing. The bind asks again
        # under the item lock, and its answer is the one that counts.
        return None, _refuse_409(
            work_ledger.CODE_DUPLICATE_ITEM,
            f"item {parsed.item_id!r} already has a worker on this crew",
            field="item_id",
        )
    widest = parsed.item("w" * mailbox.MAX_CONDUCTOR_KEY_CHARS)
    if not await asyncio.to_thread(_widest_report_fits, parsed.board, widest):
        _audit(
            _caller(request), "chat_slot_create.work_item", "denied", error="work_item_too_large"
        )
        return None, _refuse_400(
            "work_item_too_large",
            "the worker's first report must carry the whole item in one crew-log line, "
            "and this item would not fit; shrink its acceptance",
            field="acceptance",
        )
    return parsed, None


async def bind_work_item(
    request: web.Request, slot_key: str, parsed: mailbox.MailboxRequest
) -> web.Response | None:
    """Store *parsed* in the mailbox and bind *slot_key* to it; ``None`` on success.

    Called inside the create, after the slot is minted and before it is first
    broadcast, so no client ever sees it unbound. On a refusal the caller retracts
    the slot, so a create never leaves a worker that is not bound.
    """
    async with _board_lock(parsed.board):
        # A board flagged dirty is rebuild-only, so a create on it is refused
        # rather than adding a worker its own tools would then refuse.
        dirty = await _refuse_if_dirty(parsed.board, _caller(request), "chat_slot_create.work_item")
        if dirty is not None:
            return dirty
        try:
            # Drained, not shielded: a cancellation waits for the write to finish,
            # so the create's unwind sees the binding it must undo.
            await _drain_before_cancelling(
                asyncio.to_thread(mailbox.open_mailbox, parsed, slot_key)
            )
        except work_ledger.WorkLedgerError as exc:
            _audit(
                _caller(request),
                "chat_slot_create.work_item",
                "denied",
                resources=parsed.item_id,
                error=exc.code,
            )
            return _refuse_store_error(exc)
        except OSError:
            logger.warning(
                "remote work item %s: mailbox write failed", parsed.item_id, exc_info=True
            )
            return _refuse_503("ledger_write_failed", "ledger write failed; try again")
    _audit(_caller(request), "chat_slot_create.work_item", "ok", resources=parsed.item_id)
    return None


# ── the conductor's Trust, carried to its bound worker (RFC §4) ───────────


def remote_trust_scope(slot_key: str) -> str:
    """The grant key for one bound worker slot, never one for the crew."""
    return REMOTE_TRUST_SCOPE_PREFIX + slot_key


def remote_trust_active(slot_key: str) -> bool:
    """Whether *slot_key* holds a live remote grant. A pure read: expires nothing."""
    from kiro_crew.safety_override import safety_override

    scope = remote_trust_scope(slot_key)
    if remote_scope_denied(scope):
        return False
    return safety_override().scope_remaining_secs(scope) > 0


def reserve_remote_trust(slot: Any, parsed: mailbox.MailboxRequest) -> bool:
    """Reserve the remote scope on a newborn *slot* before any client can see it.

    A person's Normal or Reads from then on clears the reservation
    (``_end_trust_scopes``), and :func:`grant_remote_trust` arms only a reservation
    that survived. Until it is armed, ``is_scope_active`` answers False for it. A
    slot that already holds Trust, Reads or a scope keeps it.
    """
    if not parsed.trust:
        return False
    if (
        getattr(slot, "_trust", False)
        or getattr(slot, "_trust_reads", False)
        or getattr(slot, "_trust_scope", "")
    ):
        return False
    slot._trust_scope = remote_trust_scope(slot.key)
    return True


async def grant_remote_trust(state: Any, slot: Any, parsed: mailbox.MailboxRequest) -> bool:
    """Arm the grant reserved by :func:`reserve_remote_trust`; True when armed.

    A ``SafetyOverride`` scoped grant on this one slot (``_trust_scope``), so the
    worker auto-approves as a Trust chat's own child does. It is audited
    fail-closed at activation, lasts this crew's own ad-hoc duration
    (``agent.yolo_duration``, held to ``yolo_max_seconds``), and is refused when
    this crew's ``approval_modes`` policy denies ``trust`` or ``yolo``. The deny
    rules and ``hooks.confirm_tools`` bind it as they bind any trusted slot.
    shortcut: no renewal, so a worker past the TTL asks again; slide it on the
    hub's pull if that proves short.
    """
    scope = remote_trust_scope(slot.key)
    if not parsed.trust or getattr(slot, "_trust_scope", "") != scope:
        # Never reserved, or a person picked another mode since.
        return False
    from kiro_crew.approval_mode_policy import session_mode_permitted
    from kiro_crew.safety_override import safety_override

    operation = "chat_slot_create.work_item_trust"
    if not session_mode_permitted("trust"):
        slot._trust_scope = ""
        _audit("dashboard", operation, "denied", resources=slot.key, error="policy_denies_trust")
        return False
    result = await asyncio.to_thread(safety_override().activate_scoped, scope, "remote_dispatch")
    if not result.active:
        if slot._trust_scope == scope:
            slot._trust_scope = ""
        _audit("dashboard", operation, "denied", resources=slot.key, error="grant_refused")
        return False
    if state._slots.get(slot.key) is not slot or slot._trust_scope != scope:
        # Closed, or a person picked another mode, while the grant was armed.
        await asyncio.to_thread(safety_override().deactivate_scope, scope)
        return False
    _audit("dashboard", operation, "ok", resources=f"{slot.key} item={parsed.item_id}")
    return True


async def revoke_remote_trust(state: Any, slot_key: str) -> None:
    """End *slot_key*'s remote grant, if it holds one. Idempotent."""
    from kiro_crew.safety_override import safety_override

    scope = remote_trust_scope(slot_key)
    await asyncio.to_thread(safety_override().deactivate_scope, scope)
    slot = state._slots.get(slot_key) if state is not None else None
    if slot is not None and getattr(slot, "_trust_scope", "") == scope:
        slot._trust_scope = ""


async def discard_work_item(
    slot_key: str,
    parsed: mailbox.MailboxRequest,
    *,
    then: Callable[[], Awaitable[Any]] | None = None,
) -> None:
    """Undo :func:`bind_work_item` for a create retracted after the bind landed.

    The discard runs first and *then* (the caller's own undo) runs after it even
    when the discard fails. Both are drained as one step, so a second cancellation
    during shutdown cannot land between them and strand the binding.
    """

    async def _undo() -> None:
        try:
            async with _board_lock(parsed.board):
                await asyncio.to_thread(mailbox.discard_mailbox, parsed, slot_key)
        finally:
            if then is not None:
                await then()

    await _drain_before_cancelling(_undo())


async def close_work_item_for_slot(slot_key: str) -> None:
    """Close the hub item a deleted slot was bound to. Never raises.

    A slot closes whatever the preview says, so its item is closed whatever it says
    too. Called from the slot's own close paths (a close, a transcript delete).
    Drained, so a shutdown cancellation lands after the close. A dirty board is
    left alone (rebuild-only), and a failed write leaves the item open and logged.
    """

    async def _close() -> str | None:
        # The slot is going, so its remote grant goes first, whatever the preview
        # says. Inside the drained step, and a failed revoke still closes the item.
        try:
            await revoke_remote_trust(None, slot_key)
        except Exception:  # noqa: BLE001 - a close must never fail on the revoke
            logger.warning("remote trust for %s: could not revoke it", slot_key, exc_info=True)
        bound = await asyncio.to_thread(mailbox.bound_mailbox, slot_key)
        if bound is None:
            return None
        async with _board_lock(bound[0]):
            if await asyncio.to_thread(work_ledger.cache_dirty, bound[0]) is not None:
                return None
            return await asyncio.to_thread(mailbox.close_mailbox, slot_key)

    try:
        closed = await _drain_before_cancelling(_close())
    except (OSError, work_ledger.WorkLedgerError):
        logger.warning("remote work item for %s: could not close it", slot_key, exc_info=True)
        return
    if closed:
        _audit("dashboard", "chat_slot_work_item.close", "ok", resources=closed)


# ── the per-slot routes ───────────────────────────────────────────────────


async def _slot_mailbox(
    request: web.Request, operation: str, *, live_only: bool = True
) -> tuple[tuple[str, str] | None, web.Response | None]:
    """``(board, item_id)`` for the route's slot, or the refusal. Gate order: preview,
    owner, live slot, hub-mailbox binding.

    With *live_only* off (the read), a slot that is already closed still answers
    while its binding names a mailbox item that names it back: the worker's last
    reports stay readable for the hub after its slot is gone.
    """
    if not await _preview_on():
        # The answer for a route that does not exist, as on GET /api/chat/headroom:
        # a typed code here would tell any caller the preview is installed.
        raise web.HTTPNotFound()
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return None, denied
    slot_key = request.match_info.get("slot", "")
    live = request.app["state"]._slots.get(slot_key) is not None
    if not live and live_only:
        return None, _not_found()
    try:
        bound = await asyncio.to_thread(mailbox.bound_mailbox, slot_key)
    except work_ledger.WorkLedgerError:
        bound = None
    if bound is None:
        return None, _not_found()
    if not live and not await asyncio.to_thread(mailbox.names_worker, bound, slot_key):
        return None, _not_found()
    return bound, None


def _event_limit(request: web.Request) -> int | None:
    raw = request.query.get("limit")
    if raw is None:
        return DEFAULT_EVENT_TAIL
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= work_ledger.MAX_EVENTS_PER_ITEM else None


async def api_chat_slot_work_item_get(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/work-item — the worker's side of the hub's item."""
    bound, refusal = await _slot_mailbox(request, "chat_slot_work_item.read", live_only=False)
    if refusal is not None:
        return refusal
    assert bound is not None
    limit = _event_limit(request)
    if limit is None:
        return _refuse_400(
            "invalid_value",
            f"limit must be an integer from 1 to {work_ledger.MAX_EVENTS_PER_ITEM}",
            "limit",
        )
    board, item_id = bound
    async with _board_lock(board):
        # A dirty cache may hold a report the record refused, so it is not read,
        # as the worker's own brief is not.
        dirty = await _refuse_if_dirty(board, _caller(request), "chat_slot_work_item.read")
        if dirty is not None:
            return dirty
        found = await asyncio.to_thread(mailbox.read_mailbox, board, item_id, limit=limit)
    if found is None:
        return _not_found()
    item, events = found
    _audit(_caller(request), "chat_slot_work_item.read", "ok", resources=item_id)
    return await _redacted_json(
        {
            "work_item": {
                "item_id": item.item_id,
                "state": item.state,
                "status": item.status,
                "reason": item.reason,
                "summary": item.summary,
                "artifacts": item.artifacts,
                "pr": item.pr,
                "last_report_at": item.last_report_at,
            },
            "events": [event.to_dict() for event in events],
            # So the hub can revoke a grant its conductor has stopped backing (RFC §4).
            "trust_active": remote_trust_active(request.match_info.get("slot", "")),
        }
    )


async def api_chat_slot_work_item_patch(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/work-item — the hub's new ``decision``, and/or
    ``trust: false``, which ends the worker's remote grant (RFC §4)."""
    bound, refusal = await _slot_mailbox(request, "chat_slot_work_item.decide")
    if refusal is not None:
        return refusal
    assert bound is not None
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    extra = sorted(set(body) - {"decision", "trust"})
    if extra or not body:
        # The hub owns the decision here and nothing else: a worker-owned field
        # (status, summary, artifacts, pr, last_report_at) is the worker's to write.
        return _refuse_400(
            "invalid_value",
            "this route takes decision, trust: false, or both",
            field=extra[0] if extra else "decision",
        )
    if "trust" in body and body["trust"] is not False:
        # A grant is armed only at create; the hub may only end one.
        return _refuse_400("invalid_value", "trust may only be false", field="trust")
    board, item_id = bound
    if "trust" in body:
        slot_key = request.match_info.get("slot", "")
        await revoke_remote_trust(request.app["state"], slot_key)
        _audit(_caller(request), "chat_slot_work_item.trust_revoke", "ok", resources=item_id)
        if "decision" not in body:
            return web.json_response({"ok": True, "item_id": item_id, "trust_active": False})
    async with _board_lock(board):
        dirty = await _refuse_if_dirty(board, _caller(request), "chat_slot_work_item.decide")
        if dirty is not None:
            return dirty
        try:
            item, _event = await asyncio.to_thread(
                mailbox.write_decision,
                board,
                item_id,
                body["decision"],
                fits=lambda candidate: _widest_report_fits(board, candidate),
            )
        except work_ledger.WorkLedgerError as exc:
            _audit(
                _caller(request),
                "chat_slot_work_item.decide",
                "denied",
                resources=item_id,
                error=exc.code,
            )
            return _refuse_store_error(exc)
        except OSError:
            logger.warning("remote work item %s: decision write failed", item_id, exc_info=True)
            return _refuse_503("ledger_write_failed", "ledger write failed; try again")
    _audit(_caller(request), "chat_slot_work_item.decide", "ok", resources=item_id)
    return await _redacted_json(
        {"ok": True, "item_id": item.item_id, "decided_at": item.decided_at}
    )


__all__ = [
    "api_chat_slot_work_item_get",
    "api_chat_slot_work_item_patch",
    "bind_work_item",
    "work_item_for_create",
]

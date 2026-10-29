"""Adapters between the admission gate and the entry points that call it.

Each adapter is the whole of what one entry point adds: build the request from
what the handler already validated, call the gate, and translate the answer
into that entry point's own response or bookkeeping. They hold no state; the
gate (``admission_gate``) holds all of it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Coroutine

from kiro_crew.shared_crew import admission_gate as _gate
from kiro_crew.shared_crew.admission import (
    TASKQ_STATE_MAP,
    AcceptedWork,
    OperationKind,
    OperationState,
    ReasonCode,
)

logger = logging.getLogger(__name__)

#: Response header naming the accepted work of a streamed chat send.
WORK_ID_HEADER = "X-Shared-Crew-Work"
WORK_STATE_HEADER = "X-Shared-Crew-Work-State"


# -- chat turns ------------------------------------------------------------------

#: Slot keys whose dispatched turn is still waiting for admission.
_PENDING_SLOTS: set[str] = set()


def mark_admission_pending(slot_key: str, pending: bool) -> None:
    if pending:
        _PENDING_SLOTS.add(slot_key)
    else:
        _PENDING_SLOTS.discard(slot_key)


def admission_pending(slot_key: str) -> bool:
    """Whether *slot_key*'s turn is accepted but not yet started. In memory."""
    return slot_key in _PENDING_SLOTS


#: Work payload key holding the prompt a chat turn was accepted with.
CHAT_PROMPT_PAYLOAD_KEY = "prompt"
#: Work payload key holding that prompt's author, in the public principal shape.
CHAT_AUTHOR_PAYLOAD_KEY = "author"
#: Work payload key identifying the accepted user row independently of its text.
CHAT_SEND_ID_PAYLOAD_KEY = "sendId"


def _chat_payload(request: Any, message: str, send_id: str = "") -> dict[str, Any]:
    from kiro_crew.shared_crew.attribution import author_meta

    payload: dict[str, Any] = {CHAT_PROMPT_PAYLOAD_KEY: message}
    if send_id:
        payload[CHAT_SEND_ID_PAYLOAD_KEY] = send_id
    actor = _gate.actor_of(request)
    if actor is not None:
        payload[CHAT_AUTHOR_PAYLOAD_KEY] = author_meta(actor)
    return payload


async def admit_chat_turn(
    request: Any, *, session_key: str, slot_key: str, message: str, send_id: str = ""
) -> _gate.Admission:
    """Admit one chat turn charged to the request's initiator.

    The prompt is stored with the work (``CHAT_PROMPT_PAYLOAD_KEY``) until the
    work ends, so a crash before the session saves its user row still leaves
    the accepted text for reconciliation to put back in the session. No chat
    relauncher exists, so the payload is never replayed.
    """
    service = await _gate.current_service()
    return await service.admit(
        budget_key=_gate.actor_budget(request),
        session_key=session_key or slot_key,
        operation=OperationKind.CHAT_TURN,
        idempotency_key=_gate.idempotency_key_of(request),
        digest=chat_digest(slot_key, message),
        request_bytes=len(message.encode("utf-8", "replace")),
        payload=_chat_payload(request, message, send_id),
    )


#: Queue-entry meta key naming the parked work a message sent to a busy session holds.
QUEUED_WORK_META_KEY = "sharedCrewWork"


def admission_enforced() -> bool:
    """Whether the installed service enforces fair admission. In memory."""
    service = _gate.installed_service()
    return service is not None and service.enabled


def explicit_idempotency_key(request: Any) -> str:
    """The client's own ``Idempotency-Key`` when it sent a well-formed one, else ``""``."""
    from kiro_crew.shared_crew.admission import validate_idempotency_key
    from kiro_crew.shared_crew.codec import ContractError

    headers = getattr(request, "headers", None)
    raw = headers.get(_gate.IDEMPOTENCY_HEADER, "") if headers is not None else ""
    key = raw.strip() if isinstance(raw, str) else ""
    if not key:
        return ""
    try:
        return validate_idempotency_key(key)
    except ContractError:
        return ""


def chat_digest(slot_key: str, message: str) -> str:
    """The request digest every chat send path records, so a retry matches either path."""
    return _gate.request_digest("chat", slot_key, message)


async def chat_replay(
    request: Any, *, session_key: str, slot_key: str, message: str
) -> _gate.Admission | None:
    """The recorded answer to a retried chat send, or None when the send is new.

    Only a client-supplied key can match (a server key is fresh every time).
    Reads only: the caller answers with :func:`chat_replay_response` and
    writes no row, queues nothing and starts nothing.
    """
    key = explicit_idempotency_key(request)
    if not key:
        return None
    service = _gate.installed_service()
    if service is None or not service.enabled:
        return None
    service = await _gate.current_service()
    return await service.lookup(
        budget_key=_gate.actor_budget(request),
        session_key=session_key or slot_key,
        idempotency_key=key,
        digest=chat_digest(slot_key, message),
    )


def chat_replay_response(admission: _gate.Admission, *, slot_key: str) -> Any:
    """The JSON answer to a retried send: the recorded work, never a second stream."""
    from aiohttp import web

    if admission.rejected:
        return admission.refusal_response()
    work = admission.work
    body: dict[str, Any] = {"ok": True, "slot": slot_key, "replayed": True}
    receipt = admission_receipt(admission)
    if receipt is not None:
        body["admission"] = receipt
    if work is not None and work.state is OperationState.QUEUED:
        body["queued"] = True
        queue_id = admission.service.queue_id_for(work.work_id) if admission.service else ""
        if queue_id:
            body["queue_id"] = queue_id
    return web.json_response(body, headers=admission_headers(admission))


async def admit_queued_send(
    request: Any, *, session_key: str, slot_key: str, message: str
) -> _gate.Admission:
    """Accept a message sent to a busy session as parked work charged to its sender.

    Bounded by the sender's and the Crew's queue depth and by the request size,
    and idempotent under the client's key; the session's drain starts it
    through :func:`kiro_crew.shared_crew.turn_gate.gate_turn`. A pass-through
    with fair admission off.
    """
    if _gate.installed_service() is None:
        return _gate.Admission(None, "passthrough")
    service = await _gate.current_service()
    return await service.admit(
        budget_key=_gate.actor_budget(request),
        session_key=session_key or slot_key,
        operation=OperationKind.CHAT_TURN,
        idempotency_key=_gate.idempotency_key_of(request),
        digest=chat_digest(slot_key, message),
        request_bytes=len(message.encode("utf-8", "replace")),
        park=True,
    )


def stamp_queued_work(slot: Any, queue_id: str, admission: _gate.Admission) -> None:
    """Name *admission*'s parked work on the queue entry *queue_id*, for the drain."""
    if admission.passthrough or admission.work is None or not queue_id:
        return
    work_id = admission.work.work_id
    if admission.service is not None:
        admission.service.note_queue_id(work_id, queue_id)
    for entry in getattr(slot, "_queue", None) or ():
        if isinstance(entry, dict) and entry.get("id") == queue_id:
            meta = entry.get("meta")
            if not isinstance(meta, dict):
                meta = entry["meta"] = {}
            meta[QUEUED_WORK_META_KEY] = work_id
            return


def queued_work_ids(entries: Any) -> list[str]:
    """The parked work ids a drain's consumed queue entries carry, in order."""
    ids: list[str] = []
    for entry in entries or ():
        meta = entry.get("meta") if isinstance(entry, dict) else None
        work_id = meta.get(QUEUED_WORK_META_KEY) if isinstance(meta, dict) else None
        if isinstance(work_id, str) and work_id and work_id not in ids:
            ids.append(work_id)
    return ids


def admission_receipt(admission: _gate.Admission) -> dict[str, Any] | None:
    """The ``admission`` key a send receipt carries, or None with admission off."""
    if admission.passthrough or admission.work is None:
        return None
    return {"state": admission.work.state.value, "work": admission.work.to_public_dict()}


def admission_headers(admission: _gate.Admission) -> dict[str, str]:
    if admission.passthrough or admission.work is None:
        return {}
    return {WORK_ID_HEADER: admission.work.work_id, WORK_STATE_HEADER: admission.work.state.value}


async def admitted_chat_turn(
    slot_key: str,
    admission: _gate.Admission,
    turn: Coroutine[Any, Any, Any],
    *,
    still_live: Callable[[], bool],
    on_ended: Callable[[], Any],
) -> Any:
    """Run a dispatched chat turn through its admission: wait, revalidate, run.

    While it waits the slot is marked pending (:func:`admission_pending`) so a
    later send cannot steer into a turn that has not started. Right before the
    turn runs the slot must still be live; a closed slot ends the work unstarted.
    """
    mark_admission_pending(slot_key, True)

    async def _revalidate() -> ReasonCode | None:
        mark_admission_pending(slot_key, False)
        return None if still_live() else ReasonCode.CANCELLED_BY_USER

    try:
        return await admission.run(turn, revalidate=_revalidate)
    except _gate.AdmissionEnded as ended:
        logger.info(
            "shared crew: turn %s ended before it ran (%s, %s)",
            ended.work.work_id,
            ended.work.state.value,
            ended.work.reason.value if ended.work.reason else "",
        )
        on_ended()
        return None
    finally:
        mark_admission_pending(slot_key, False)


def admitted_or_direct(
    slot_key: str,
    admission: _gate.Admission,
    turn: Coroutine[Any, Any, Any],
    *,
    still_live: Callable[[], bool],
    on_ended: Callable[[], Any],
) -> Coroutine[Any, Any, Any]:
    """*turn* itself when *admission* is a pass-through, else :func:`admitted_chat_turn`."""
    if admission.passthrough:
        return turn
    return admitted_chat_turn(slot_key, admission, turn, still_live=still_live, on_ended=on_ended)


# -- subagents -------------------------------------------------------------------


def subagent_probe(manager: Any) -> Callable[[str], OperationState | None]:
    """A child probe answering a subagent run's state from the manager, then the task row.

    None means still running (or not decidable yet). Runs off-loop (the gate
    calls it through ``asyncio.to_thread``), so the task-row read is allowed.
    """

    def probe(agent_id: str) -> OperationState | None:
        agents = getattr(manager, "_agents", None)
        info = agents.get(agent_id) if isinstance(agents, dict) else None
        if info is not None:
            if not getattr(info, "done", False):
                return None
            if getattr(info, "user_stopped", False):
                return OperationState.CANCELLED
            return OperationState.FAILED if getattr(info, "error", "") else OperationState.COMPLETED
        store = None
        try:
            store = manager.taskq_store()
        except Exception:
            store = None
        if store is not None:
            try:
                row = store.get(agent_id)
            except Exception:
                row = None
            if row is not None:
                mapped = TASKQ_STATE_MAP.get(str(row.state))
                return mapped if mapped is not None and mapped.value in _TERMINAL_VALUES else None
        # Neither the manager nor the queue knows it: it may have run, and is gone.
        return OperationState.UNKNOWN_OUTCOME

    return probe


_TERMINAL_VALUES = frozenset(
    {"completed", "failed", "cancelled", "expired", "rejected", "unknown_outcome"}
)


def subagent_cancel(manager: Any, loop: Any) -> Callable[[AcceptedWork], None]:
    """A child cancel hook: stop the subagent run the record names."""

    async def stop_child(work: AcceptedWork, agent_id: str) -> None:
        # Queued stops hold parent refill while the store writer cancels the row.
        if await manager.cancel_queued(agent_id, work.session_key):
            return
        info = manager._agents.get(agent_id)
        starting = manager._admission.starting_claim_of(agent_id)
        if starting is not None or (info is not None and not info.done and not info.queued):
            await manager.cancel(agent_id)

    def cancel(work: AcceptedWork) -> None:
        agent_id = str(work.extra.get("execution_id") or "")
        stop = getattr(manager, "cancel", None)
        if not agent_id or stop is None:
            return
        try:
            loop.create_task(stop_child(work, agent_id))
        except Exception:
            logger.debug("shared crew: subagent cancel failed", exc_info=True)

    return cancel


async def admit_subagent(request: Any, state: Any, parent_session: str) -> _gate.Admission:
    """Admit a spawn as a child of whatever runs in *parent_session*."""
    service = await _gate.current_service()
    if not service.enabled:
        return _gate.Admission(service, "passthrough")
    manager = getattr(state, "subagents", None)
    if manager is not None:
        service.set_child_probe(subagent_probe(manager))
        service.set_child_cancel(subagent_cancel(manager, asyncio.get_running_loop()))
    internal = request.get("internal_auth") is True
    if internal:
        await _gate.attest_internal_session(request)
    admission = await service.admit_child(
        parent_session_key=parent_session,
        fallback_budget=_gate.actor_budget(request, automation=internal),
    )
    if admission.admitted and admission.work is not None:
        await service.prepare_principal_slice(admission.work.budget_key)
    return admission


async def admit_app_spawn(manager: Any, app: str) -> _gate.Admission:
    """Admit an app's background spawn (``apps.spawn_sdk``) as a child.

    An app spawns with no request and no parent turn, so the child is charged
    to the app's bounded system principal, the one ``actor_budget`` names for
    an app token. The caller attaches the started run or abandons the slot.
    First use reads config off-loop; disabled admission builds no service.
    """
    if _gate.installed_service() is None:
        from kiro_crew.config.sections_shared_crew import load_shared_crew_config

        config = await asyncio.to_thread(load_shared_crew_config)
        if not config.admission_enabled:
            return _gate.Admission(None, "passthrough")
    service = await _gate.current_service()
    if not service.enabled:
        return _gate.Admission(service, "passthrough")
    if manager is not None:
        service.set_child_probe(subagent_probe(manager))
        service.set_child_cancel(subagent_cancel(manager, asyncio.get_running_loop()))
    from kiro_crew.shared_crew.contracts import Principal

    admission = await service.admit_child(
        parent_session_key=f"app:{app}",
        fallback_budget=Principal.system(f"app:{app}"[:64]).id,
    )
    if admission.admitted and admission.work is not None:
        await service.prepare_principal_slice(admission.work.budget_key)
    return admission


# -- cron -------------------------------------------------------------------------


async def admitted_cron_dispatch(job: Any, dispatch: Coroutine[Any, Any, Any]) -> Any:
    """Run one cron message dispatch through fair admission.

    Charged to the principal the job recorded as its initiator, else to the
    bounded automation owner. A refused or expired fire is skipped like a
    governance denial: recorded on the job, never fed to the auto-pause counter.
    With fair admission off this awaits *dispatch* unchanged. A retry inside
    admitted work retains its outer reservation and deadline.
    """
    from kiro_crew.shared_crew.turn_gate import admitted_work

    if admitted_work():
        return await dispatch
    try:
        return await _gate.admit_and_run(
            dispatch,
            budget_key=_gate.recorded_principal(getattr(job, "created_by_principal", "")),
            session_key=f"cron:{job.id}",
            operation=OperationKind.CRON,
        )
    except _gate.AdmissionRefused as refused:
        reason = refused.admission.reason
        job.last_status = "error"
        job.last_error = (
            f"fair admission refused this run ({reason.value if reason else 'refused'})"
        )
    except _gate.AdmissionEnded as ended:
        job.last_status = "error"
        job.last_error = (
            f"fair admission ended this run before it started ({ended.work.state.value})"
        )
    logger.info("cron %s: %s", getattr(job, "id", "?"), job.last_error)
    return None


# -- background runs (task runner, workflows) ---------------------------------------

_TASKRUNNER_TERMINAL: dict[str, OperationState] = {
    "completed": OperationState.COMPLETED,
    "done": OperationState.COMPLETED,
    "failed": OperationState.FAILED,
    "cancelled": OperationState.CANCELLED,
    # A paused run holds no runtime; a restart is new work.
    "paused": OperationState.CANCELLED,
}

_WORKFLOW_TERMINAL: dict[str, OperationState] = {
    "finished": OperationState.COMPLETED,
    "failed": OperationState.FAILED,
    "cancelled": OperationState.CANCELLED,
    "paused": OperationState.CANCELLED,
}


def taskrunner_probe(runner: Any) -> Callable[[str], OperationState | None]:
    def probe(task_id: str) -> OperationState | None:
        runs = getattr(runner, "_runs", None)
        run = runs.get(task_id) if isinstance(runs, dict) else None
        if run is None:
            return OperationState.UNKNOWN_OUTCOME
        return _TASKRUNNER_TERMINAL.get(str(getattr(run, "status", "")))

    return probe


def workflow_probe(service: Any) -> Callable[[str], OperationState | None]:
    def probe(run_id: str) -> OperationState | None:
        try:
            snap = service.status(run_id)
        except Exception:
            return None
        if not isinstance(snap, dict):
            return OperationState.UNKNOWN_OUTCOME
        return _WORKFLOW_TERMINAL.get(str(snap.get("status") or ""))

    return probe


def _cancel_hook(cancel: Callable[[str], Any]) -> Callable[[AcceptedWork], None]:
    def hook(work: AcceptedWork) -> None:
        run_id = str(work.extra.get("execution_id") or "")
        if not run_id:
            return
        try:
            result = cancel(run_id)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)
        except Exception:
            logger.debug("shared crew: cancelling run %s failed", run_id, exc_info=True)

    return hook


async def admit_background_run(
    request: Any,
    *,
    operation: OperationKind,
    session_key: str,
    probe: Callable[[str], OperationState | None] | None,
    cancel: Callable[[str], Any] | None = None,
    body: object = None,
) -> _gate.Admission:
    """Admit a background run that starts now or is refused; it never queues here.

    *body* joins the replay digest, so a retry under the same key with a
    different body is an idempotency conflict, as a chat send is.

    The caller starts the run only when admitted, then calls
    ``attach_child(run_id)``; the gate's tick settles the record through
    *probe* and enforces the run deadline through *cancel*.
    """
    service = await _gate.current_service()
    if not service.enabled:
        return _gate.Admission(service, "passthrough")
    if probe is not None:
        service.register_probe(operation, probe)
    if cancel is not None:
        service.register_cancel(operation, _cancel_hook(cancel))
    internal = request.get("internal_auth") is True
    if internal:
        await _gate.attest_internal_session(request)
    admission = await service.admit(
        budget_key=_gate.actor_budget(request, automation=internal),
        session_key=session_key or operation.value,
        operation=operation,
        idempotency_key=_gate.idempotency_key_of(request),
        digest=_gate.request_digest(operation.value, session_key, body),
        allow_queue=False,
    )
    if admission.admitted and not admission.replayed and admission.work is not None:
        await service.prepare_principal_slice(admission.work.budget_key)
    return admission


def background_replay_response(admission: _gate.Admission, *, execution_key: str = "run_id") -> Any:
    """Return the recorded run, or an accepted receipt while its start is in flight.

    A retry owns neither the original start nor its capacity and must not start,
    attach or abandon it. The work receipt remains available after a failed start.
    """
    from aiohttp import web

    body: dict[str, Any] = {"ok": True, "replayed": True}
    receipt = admission_receipt(admission)
    if receipt is not None:
        body["admission"] = receipt
    work = admission.work
    execution_id = str(work.extra.get("execution_id") or "") if work is not None else ""
    if execution_id:
        body[execution_key] = execution_id
    pending = work is not None and work.state in {
        OperationState.ADMITTED,
        OperationState.QUEUED,
    }
    if pending and not execution_id:
        return web.json_response(body, status=202)
    return web.json_response(body)


__all__ = [
    "background_replay_response",
    "CHAT_AUTHOR_PAYLOAD_KEY",
    "CHAT_PROMPT_PAYLOAD_KEY",
    "CHAT_SEND_ID_PAYLOAD_KEY",
    "QUEUED_WORK_META_KEY",
    "admission_enforced",
    "admit_queued_send",
    "chat_digest",
    "chat_replay",
    "chat_replay_response",
    "explicit_idempotency_key",
    "queued_work_ids",
    "stamp_queued_work",
    "admit_app_spawn",
    "admit_background_run",
    "admitted_chat_turn",
    "admitted_or_direct",
    "admitted_cron_dispatch",
    "taskrunner_probe",
    "workflow_probe",
    "WORK_ID_HEADER",
    "WORK_STATE_HEADER",
    "admission_headers",
    "admission_pending",
    "admission_receipt",
    "admit_chat_turn",
    "admit_subagent",
    "mark_admission_pending",
    "subagent_cancel",
    "subagent_probe",
]

"""Durable :class:`AcceptedWork` records over the task store.

Each record is one ``accepted_work`` row in ``tasks.db`` (``kiro_crew.taskq.store_work``),
written through the task store's single writer thread. Two properties carry the
admission guarantees:

* Write before acknowledge. :meth:`WorkStore.accept` returns only after the row
  is committed, so an entry point never tells a caller "queued" or "admitted"
  for work a crash could lose.
* Idempotency in the store, not in memory. The row's ``scope`` column is
  unique (``idempotency_scope(budget_key, session_key, key)``), and the lookup
  and insert are one transaction, so a retry (from the same tab, another tab or
  after a restart) reads back the first record instead of creating new work.
  A retry whose request digest differs is :class:`IdempotencyConflict`.

Every state change is a compare-and-set on ``revision`` that also passes the
contract's transition table (:meth:`AcceptedWork.advance`), so two writers
cannot both move one record and an illegal transition never reaches disk.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from kiro_crew.shared_crew.admission import (
    IDEMPOTENCY_TTL_SECONDS,
    TERMINAL,
    AcceptedWork,
    AdmissionRequest,
    OperationState,
    ReasonCode,
    RuntimeOwnershipRef,
    StaleRevision,
)
from kiro_crew.shared_crew.codec import ContractError

if TYPE_CHECKING:
    from kiro_crew.taskq.store import TaskStore, WorkRow

logger = logging.getLogger(__name__)

#: Largest stored replay payload, in bytes of JSON. A larger one is not stored,
#: so the work cannot be relaunched after a restart and ends ``cancelled``.
MAX_PAYLOAD_BYTES = 256 * 1024


class IdempotencyConflict(ContractError):
    """A retry reused an idempotency key for a different request."""

    def __init__(self, current: AcceptedWork) -> None:
        super().__init__(
            ReasonCode.IDEMPOTENCY_CONFLICT.value, "idempotency_key", "key reused for other work"
        )
        self.current = current


#: Called on the event loop with every record a store wrote; see :func:`set_change_listener`.
_CHANGE_LISTENER: Callable[[AcceptedWork], None] | None = None


def set_change_listener(listener: Callable[[AcceptedWork], None] | None) -> None:
    """Hear every accepted or advanced record (gateway wiring pushes it to tabs). None removes."""
    global _CHANGE_LISTENER
    _CHANGE_LISTENER = listener


def _changed(work: AcceptedWork) -> None:
    listener = _CHANGE_LISTENER
    if listener is None:
        return
    try:
        listener(work)
    except Exception:  # noqa: BLE001 -- a listener never fails the write it heard
        logger.debug("shared crew: work change listener failed", exc_info=True)


def new_work_id() -> str:
    return "w_" + uuid.uuid4().hex


def _encode(work: AcceptedWork) -> str:
    return json.dumps(work.to_dict(), sort_keys=True, separators=(",", ":"))


def _decode(row: WorkRow) -> AcceptedWork:
    return AcceptedWork.from_dict(json.loads(row.record_json))


class WorkStore:
    """Accepted-work persistence. Every coroutine runs its SQLite on the writer thread."""

    def __init__(self, tasks: TaskStore, *, clock: Callable[[], float] = time.time) -> None:
        self._tasks = tasks
        self._clock = clock

    @property
    def tasks(self) -> TaskStore:
        return self._tasks

    # -- writes ---------------------------------------------------------------

    def accept_sync(
        self,
        request: AdmissionRequest,
        *,
        state: OperationState,
        deadline_at: float | None = None,
        payload: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        work_id: str | None = None,
    ) -> tuple[AcceptedWork, bool]:
        """Store new work, or return the record an earlier submission of its scope made."""
        now = self._clock()
        work = AcceptedWork(
            work_id=work_id or new_work_id(),
            scope=request.scope,
            budget_key=request.budget_key,
            session_key=request.session_key,
            operation=request.operation,
            state=state,
            revision=1,
            accepted_at=now,
            expires_at=request.expires_at,
            deadline_at=deadline_at,
            host_ack=self._tasks.incarnation,
            request_digest=request.request_digest,
            extra=dict(extra or {}),
        )
        payload_json: str | None = None
        if payload is not None:
            encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
                payload_json = encoded
        from kiro_crew.taskq.store import WorkRow

        row = WorkRow(
            work_id=work.work_id,
            scope=work.scope,
            budget_key=work.budget_key,
            session_key=work.session_key,
            state=work.state.value,
            revision=work.revision,
            accepted_at=now,
            updated_at=now,
            record_json=_encode(work),
            payload_json=payload_json,
        )
        stored, inserted = self._tasks.work_insert_or_get(row)
        current = _decode(stored)
        if not inserted and current.request_digest != request.request_digest:
            raise IdempotencyConflict(current)
        return current, inserted

    async def accept(
        self,
        request: AdmissionRequest,
        *,
        state: OperationState,
        deadline_at: float | None = None,
        payload: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        work_id: str | None = None,
    ) -> tuple[AcceptedWork, bool]:
        result: tuple[AcceptedWork, bool] = await self._tasks.run(
            self.accept_sync,
            request,
            state=state,
            deadline_at=deadline_at,
            payload=payload,
            extra=extra,
            work_id=work_id,
        )
        if result[1]:
            _changed(result[0])
        return result

    def advance_sync(
        self,
        work: AcceptedWork,
        new_state: OperationState,
        *,
        reason: ReasonCode | None = None,
        runtime: RuntimeOwnershipRef | None = None,
        extra: Mapping[str, Any] | None = None,
        clear_payload: bool = False,
        deadline_at: float | None = None,
    ) -> AcceptedWork:
        """Write the next revision of *work*; :class:`StaleRevision` when someone moved it."""
        nxt = work.advance(
            new_state, expected_revision=work.revision, reason=reason, runtime=runtime
        )
        if extra:
            nxt = replace(nxt, extra={**dict(work.extra), **dict(extra)})
        if deadline_at is not None:
            nxt = replace(nxt, deadline_at=deadline_at)
        ok = self._tasks.work_compare_and_set(
            work.work_id,
            expected_revision=work.revision,
            state=nxt.state.value,
            revision=nxt.revision,
            updated_at=self._clock(),
            record_json=_encode(nxt),
            clear_payload=clear_payload or nxt.state in TERMINAL,
        )
        if not ok:
            current = self.get_sync(work.work_id)
            raise StaleRevision(work.revision, current.revision if current else -1)
        return nxt

    async def advance(
        self,
        work: AcceptedWork,
        new_state: OperationState,
        *,
        reason: ReasonCode | None = None,
        runtime: RuntimeOwnershipRef | None = None,
        extra: Mapping[str, Any] | None = None,
        deadline_at: float | None = None,
    ) -> AcceptedWork:
        result: AcceptedWork = await self._tasks.run(
            self.advance_sync,
            work,
            new_state,
            reason=reason,
            runtime=runtime,
            extra=extra,
            deadline_at=deadline_at,
        )
        _changed(result)
        return result

    def purge_sync(self, *, now: float | None = None) -> int:
        """Drop terminal records older than the idempotency window."""
        at = self._clock() if now is None else now
        return int(
            self._tasks.work_purge(
                states=[s.value for s in TERMINAL], updated_before=at - IDEMPOTENCY_TTL_SECONDS
            )
        )

    # -- reads ----------------------------------------------------------------

    def get_sync(self, work_id: str) -> AcceptedWork | None:
        row = self._tasks.work_get(work_id)
        return None if row is None else _decode(row)

    async def get(self, work_id: str) -> AcceptedWork | None:
        result: AcceptedWork | None = await self._tasks.run(self.get_sync, work_id)
        return result

    def payload_sync(self, work_id: str) -> dict[str, Any] | None:
        row = self._tasks.work_get(work_id)
        if row is None or row.payload_json is None:
            return None
        value = json.loads(row.payload_json)
        return value if isinstance(value, dict) else None

    def list_sync(
        self,
        *,
        states: Iterable[OperationState] | None = None,
        session_key: str | None = None,
        budget_key: str | None = None,
        limit: int = 1000,
    ) -> list[AcceptedWork]:
        rows = self._tasks.work_rows(
            states=None if states is None else [s.value for s in states],
            session_key=session_key,
            budget_key=budget_key,
            limit=limit,
        )
        out: list[AcceptedWork] = []
        for row in rows:
            try:
                out.append(_decode(row))
            except (ContractError, ValueError):
                continue
        return out

    async def list(
        self,
        *,
        states: Iterable[OperationState] | None = None,
        session_key: str | None = None,
        budget_key: str | None = None,
        limit: int = 1000,
    ) -> list[AcceptedWork]:
        result: list[AcceptedWork] = await self._tasks.run(
            self.list_sync,
            states=None if states is None else tuple(states),
            session_key=session_key,
            budget_key=budget_key,
            limit=limit,
        )
        return result


__all__ = [
    "IdempotencyConflict",
    "MAX_PAYLOAD_BYTES",
    "WorkStore",
    "new_work_id",
    "set_change_listener",
]

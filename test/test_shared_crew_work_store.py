"""Accepted-work store: insert-or-get, compare-and-set, idempotency, purge, request helpers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from kiro_crew.shared_crew import work_store as ws
from kiro_crew.shared_crew.admission import (
    IDEMPOTENCY_TTL_SECONDS,
    AcceptedWork,
    AdmissionRequest,
    InvalidTransition,
    OperationKind,
    OperationState,
    ReasonCode,
    RuntimeOwnershipRef,
    StaleRevision,
)
from kiro_crew.shared_crew.admission_http import (
    IDEMPOTENCY_HEADER,
    coded_json_response,
    idempotency_key_of,
    request_digest,
)
from kiro_crew.shared_crew.contracts import (
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)
from kiro_crew.taskq.store import TaskStore

S = OperationState

ALICE = Principal(
    id=principal_id_for("tailscale", "example.ts.net", "alice@example.com"),
    kind=PrincipalKind.HUMAN,
    provider="tailscale",
    issuer="example.ts.net",
    subject="alice@example.com",
    provenance=Provenance.TAILSCALE_SERVE,
    display=DisplaySnapshot(name="Alice", login="alice@example.com", captured_at=10.0),
)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def tasks(tmp_path: Path) -> Iterator[TaskStore]:
    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(tasks: TaskStore, clock: _Clock) -> ws.WorkStore:
    return ws.WorkStore(tasks, clock=clock)


@pytest.fixture(autouse=True)
def _no_listener() -> Iterator[None]:
    ws.set_change_listener(None)
    yield
    ws.set_change_listener(None)


def _raw_sql(tasks: TaskStore, sql: str, *args: object) -> None:
    """Write a row the public API never writes, through a second connection."""
    conn = sqlite3.connect(tasks.path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _request(
    key: str = "key-00000001", *, digest: str = "d1", session: str = "chat-1"
) -> AdmissionRequest:
    return AdmissionRequest(
        idempotency_key=key,
        budget_key=ALICE.id,
        session_key=session,
        operation=OperationKind.CHAT_TURN,
        submitted_at=1000.0,
        expires_at=2000.0,
        request_digest=digest,
    )


# -- accept: write before acknowledge, idempotency in the store ----------------


def test_accept_stores_a_new_record_and_reads_it_back(
    store: ws.WorkStore, tasks: TaskStore
) -> None:
    work, inserted = store.accept_sync(
        _request(), state=S.QUEUED, deadline_at=1500.0, extra={"k": 1}
    )
    assert inserted is True
    assert work.work_id.startswith("w_")
    assert work.state is S.QUEUED
    assert work.revision == 1
    assert work.accepted_at == 1000.0
    assert work.deadline_at == 1500.0
    assert work.host_ack == tasks.incarnation
    assert work.extra == {"k": 1}
    assert store.get_sync(work.work_id) == work
    assert store.tasks is tasks


def test_a_retry_of_the_same_scope_returns_the_first_record(store: ws.WorkStore) -> None:
    first, inserted = store.accept_sync(_request(), state=S.QUEUED)
    again, inserted_again = store.accept_sync(_request(), state=S.ADMITTED, work_id="w_other")
    assert inserted is True
    assert inserted_again is False
    assert again == first
    assert store.get_sync("w_other") is None


def test_a_retry_with_a_different_digest_is_an_idempotency_conflict(store: ws.WorkStore) -> None:
    first, _ = store.accept_sync(_request(digest="d1"), state=S.QUEUED)
    with pytest.raises(ws.IdempotencyConflict) as err:
        store.accept_sync(_request(digest="d2"), state=S.QUEUED)
    assert err.value.current == first
    assert err.value.code == ReasonCode.IDEMPOTENCY_CONFLICT.value


def test_the_same_key_in_another_session_is_new_work(store: ws.WorkStore) -> None:
    a, _ = store.accept_sync(_request(session="chat-1"), state=S.QUEUED)
    b, inserted = store.accept_sync(_request(session="chat-2"), state=S.QUEUED)
    assert inserted is True
    assert a.work_id != b.work_id


def test_payload_is_stored_for_replay_however_large(store: ws.WorkStore) -> None:
    small, _ = store.accept_sync(_request("key-00000001"), state=S.QUEUED, payload={"text": "hi"})
    assert store.payload_sync(small.work_id) == {"text": "hi"}
    # An accepted 300 KiB prompt keeps its recovery payload.
    big = {"text": "x" * (300 * 1024)}
    large, _ = store.accept_sync(_request("key-00000002"), state=S.QUEUED, payload=big)
    assert store.payload_sync(large.work_id) == big
    none, _ = store.accept_sync(_request("key-00000003"), state=S.QUEUED)
    assert store.payload_sync(none.work_id) is None
    assert store.payload_sync("w_missing") is None


def test_a_payload_that_is_not_an_object_reads_as_none(
    store: ws.WorkStore, tasks: TaskStore
) -> None:
    work, _ = store.accept_sync(_request(), state=S.QUEUED, payload={"a": 1})
    _raw_sql(tasks, "UPDATE accepted_work SET payload_json=? WHERE work_id=?", "[1]", work.work_id)
    assert store.payload_sync(work.work_id) is None


# -- advance: compare-and-set on revision, transition table enforced -----------


def test_advance_writes_the_next_revision(store: ws.WorkStore, clock: _Clock) -> None:
    work, _ = store.accept_sync(_request(), state=S.QUEUED, payload={"text": "hi"}, extra={"a": 1})
    clock.now = 1100.0
    runtime = RuntimeOwnershipRef(execution_id="e1", lease="l1", tenancy_epoch=2)
    nxt = store.advance_sync(work, S.ADMITTED, runtime=runtime, extra={"b": 2}, deadline_at=1800.0)
    assert nxt.revision == 2
    assert nxt.state is S.ADMITTED
    assert nxt.extra == {"a": 1, "b": 2}
    assert nxt.deadline_at == 1800.0
    assert nxt.runtime == runtime
    assert store.get_sync(work.work_id) == nxt
    assert store.payload_sync(work.work_id) == {"text": "hi"}


def test_a_terminal_advance_clears_the_payload(store: ws.WorkStore) -> None:
    work, _ = store.accept_sync(_request(), state=S.QUEUED, payload={"text": "hi"})
    done = store.advance_sync(work, S.CANCELLED, reason=ReasonCode.IDEMPOTENCY_CONFLICT)
    assert done.state is S.CANCELLED
    assert store.payload_sync(work.work_id) is None


def test_clear_payload_drops_it_on_a_non_terminal_advance(store: ws.WorkStore) -> None:
    work, _ = store.accept_sync(_request(), state=S.QUEUED, payload={"text": "hi"})
    store.advance_sync(work, S.ADMITTED, clear_payload=True)
    assert store.payload_sync(work.work_id) is None


def test_a_stale_revision_never_overwrites_the_newer_one(store: ws.WorkStore) -> None:
    work, _ = store.accept_sync(_request(), state=S.QUEUED)
    winner = store.advance_sync(work, S.ADMITTED)
    with pytest.raises(StaleRevision):
        store.advance_sync(work, S.CANCELLED)
    assert store.get_sync(work.work_id) == winner


def test_a_stale_advance_of_a_purged_record_reports_minus_one(
    store: ws.WorkStore, tasks: TaskStore
) -> None:
    work, _ = store.accept_sync(_request(), state=S.QUEUED)
    _raw_sql(tasks, "DELETE FROM accepted_work WHERE work_id=?", work.work_id)
    with pytest.raises(StaleRevision) as err:
        store.advance_sync(work, S.ADMITTED)
    assert "current -1" in str(err.value)


def test_an_illegal_transition_never_reaches_disk(store: ws.WorkStore) -> None:
    work, _ = store.accept_sync(_request(), state=S.QUEUED)
    with pytest.raises(InvalidTransition):
        store.advance_sync(work, S.COMPLETED)
    assert store.get_sync(work.work_id) == work


# -- purge and list --------------------------------------------------------------


def test_purge_drops_only_terminal_records_past_the_window(
    store: ws.WorkStore, clock: _Clock
) -> None:
    old, _ = store.accept_sync(_request("key-00000001"), state=S.QUEUED)
    store.advance_sync(old, S.CANCELLED)
    live, _ = store.accept_sync(_request("key-00000002"), state=S.QUEUED)
    assert store.purge_sync() == 0
    clock.now = 1000.0 + IDEMPOTENCY_TTL_SECONDS + 1
    assert store.purge_sync() == 1
    assert store.get_sync(old.work_id) is None
    assert store.get_sync(live.work_id) == live
    assert store.purge_sync(now=0.0) == 0


def test_list_filters_by_state_session_and_budget(store: ws.WorkStore) -> None:
    a, _ = store.accept_sync(_request("key-00000001", session="chat-1"), state=S.QUEUED)
    b, _ = store.accept_sync(_request("key-00000002", session="chat-2"), state=S.ADMITTED)
    assert {w.work_id for w in store.list_sync()} == {a.work_id, b.work_id}
    assert [w.work_id for w in store.list_sync(states=[S.QUEUED])] == [a.work_id]
    assert [w.work_id for w in store.list_sync(session_key="chat-2")] == [b.work_id]
    assert store.list_sync(budget_key="someone-else") == []
    assert len(store.list_sync(limit=1)) == 1


def test_list_skips_a_record_that_does_not_decode(store: ws.WorkStore, tasks: TaskStore) -> None:
    good, _ = store.accept_sync(_request("key-00000001"), state=S.QUEUED)
    bad, _ = store.accept_sync(_request("key-00000002"), state=S.QUEUED)
    _raw_sql(
        tasks,
        "UPDATE accepted_work SET record_json=? WHERE work_id=?",
        json.dumps({"state": "nope"}),
        bad.work_id,
    )
    assert [w.work_id for w in store.list_sync()] == [good.work_id]


# -- the async surface and the change listener ------------------------------------


def test_async_surface_runs_on_the_writer_and_notifies_the_listener(store: ws.WorkStore) -> None:
    heard: list[AcceptedWork] = []
    ws.set_change_listener(heard.append)

    async def scenario() -> None:
        work, inserted = await store.accept(_request(), state=S.QUEUED, payload={"t": 1})
        assert inserted is True
        again, inserted_again = await store.accept(_request(), state=S.QUEUED)
        assert inserted_again is False and again == work
        nxt = await store.advance(work, S.ADMITTED, extra={"x": 1}, deadline_at=1900.0)
        assert await store.get(work.work_id) == nxt
        assert [w.work_id for w in await store.list(states=[S.ADMITTED])] == [work.work_id]
        assert await store.list() == [nxt]

    asyncio.run(scenario())
    # The replay heard nothing: only the insert and the advance changed a record.
    assert [w.state for w in heard] == [S.QUEUED, S.ADMITTED]


def test_a_failing_listener_never_fails_the_write(store: ws.WorkStore) -> None:
    def boom(_work: AcceptedWork) -> None:
        raise RuntimeError("listener down")

    ws.set_change_listener(boom)

    async def scenario() -> AcceptedWork:
        work, _ = await store.accept(_request(), state=S.QUEUED)
        return work

    work = asyncio.run(scenario())
    assert store.get_sync(work.work_id) == work


# -- admission_http: request-side helpers ------------------------------------------


def test_idempotency_key_is_the_header_or_a_fresh_server_key() -> None:
    assert idempotency_key_of(SimpleNamespace(headers={IDEMPOTENCY_HEADER: " k-1 "})) == "k-1"
    blank = idempotency_key_of(SimpleNamespace(headers={IDEMPOTENCY_HEADER: "  "}))
    assert blank.startswith("srv-w_")
    assert idempotency_key_of(SimpleNamespace(headers=None)).startswith("srv-w_")
    assert idempotency_key_of(object()).startswith("srv-w_")
    assert idempotency_key_of(SimpleNamespace(headers={})) != idempotency_key_of(
        SimpleNamespace(headers={})
    )


def test_request_digest_is_stable_and_separates_parts() -> None:
    assert request_digest("a", 1) == request_digest("a", 1)
    assert request_digest("ab", "c") != request_digest("a", "bc")
    assert len(request_digest()) == 64


@pytest.mark.parametrize(
    "extra",
    [
        {"capacity": {"n": 1}, "current": {"n": 2}},
        {"capacity": {"n": 1}},
        {"current": {"n": 2}},
        {},
    ],
)
def test_coded_json_response_writes_error_code_and_counts(extra: dict[str, Any]) -> None:
    body = {"error": "busy", "code": "capacity_exhausted", **extra}
    resp = coded_json_response(body, status=429, headers={"Retry-After": "3"})
    assert resp.status == 429
    assert resp.headers["Retry-After"] == "3"
    assert json.loads(resp.text) == body


def test_coded_json_response_without_headers() -> None:
    resp = coded_json_response({"error": "e", "code": "c"}, status=400)
    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "e", "code": "c"}


# -- the task-store table writers the store sits on ----------------------------------


def test_work_by_scope_names_the_stored_work(store: ws.WorkStore, tasks: TaskStore) -> None:
    req = _request()
    work, _ = store.accept_sync(req, state=S.QUEUED)
    assert tasks.work_by_scope(req.scope) == work.work_id
    assert tasks.work_by_scope("no-such-scope") is None


def test_work_rows_with_an_empty_state_filter_is_empty(
    store: ws.WorkStore, tasks: TaskStore
) -> None:
    store.accept_sync(_request(), state=S.QUEUED)
    assert tasks.work_rows(states=[]) == []
    assert store.list_sync(states=[]) == []
    assert tasks.work_purge(states=[], updated_before=1e12) == 0


def test_storage_ledger_adds_and_never_goes_below_zero(tasks: TaskStore) -> None:
    assert tasks.storage_bytes(ALICE.id) == 0
    assert tasks.storage_charge(ALICE.id, 100, at=1.0) == 100
    assert tasks.storage_charge(ALICE.id, 50, at=2.0) == 150
    assert tasks.storage_charge(ALICE.id, -400, at=3.0) == 0
    assert tasks.storage_bytes(ALICE.id) == 0
    # A first write with a negative delta starts the ledger at zero.
    assert tasks.storage_charge("other", -5, at=1.0) == 0

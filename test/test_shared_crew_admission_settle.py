"""``admission_settle``: terminal writes the store refused, retried by the ticker."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.shared_crew import admission_settle as settle
from kiro_crew.shared_crew.admission import (
    InvalidTransition,
    OperationState,
    ReasonCode,
    StaleRevision,
)

S = OperationState


@dataclass(frozen=True)
class _Work:
    work_id: str
    state: OperationState
    reason: ReasonCode | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _live(state=S.RUNNING, **kw) -> SimpleNamespace:
    base = {"unpersisted": None, "merged_into": "", "stopping_child": ""}
    base.update(kw)
    return SimpleNamespace(work=_Work("w1", state), **base)


class _Store:
    def __init__(self, advance=None, get=None):
        self._advance = advance
        self._get = get
        self.calls: list[Any] = []

    async def advance(self, work, state, **kwargs):
        self.calls.append(state)
        if self._advance is not None:
            return self._advance(work, state)
        return _Work(work.work_id, state, kwargs.get("reason"))

    async def get(self, work_id):
        return self._get(work_id) if self._get is not None else None


def _service(store, *lives, probe=None):
    released: list[str] = []
    svc = SimpleNamespace(
        _store=store,
        _live={f"w{i}": live for i, live in enumerate(lives, 1)},
        _stopped_ref=lambda live: None,
        _probe_for=lambda live: probe,
        _maybe_release=lambda live: released.append(live.work.work_id),
    )
    return svc, released


def test_a_second_failure_keeps_the_first_durable_record():
    live = _live()
    settle.mark_unpersisted(live, S.FAILED, ReasonCode.RUNTIME_ERROR, {"a": 1})
    first = live.unpersisted[0]
    settle.mark_unpersisted(live, S.CANCELLED, None, {"b": 2})
    assert live.unpersisted[0] is first
    assert live.unpersisted[1:] == (S.CANCELLED, None, {"a": 1, "b": 2})
    assert live.work.state is S.CANCELLED and live.work.extra == {"a": 1, "b": 2}


def test_the_target_falls_back_to_a_legal_end():
    assert settle._reachable(S.RUNNING, S.FAILED) is S.FAILED
    assert settle._reachable(S.QUEUED, S.FAILED) is S.CANCELLED
    assert settle._reachable(S.COMPLETED, S.FAILED) is S.FAILED


@pytest.mark.asyncio
async def test_nothing_to_persist_without_a_store():
    svc, _ = _service(None)
    assert await settle.persist_pending(svc) == 0


@pytest.mark.asyncio
async def test_an_already_terminal_durable_record_is_adopted():
    live = _live(S.COMPLETED)
    settle.mark_unpersisted(live, S.FAILED, ReasonCode.RUNTIME_ERROR)
    store = _Store()
    svc, released = _service(store, live)
    assert await settle.persist_pending(svc) == 1
    assert store.calls == [] and live.unpersisted is None and released == ["w1"]


@pytest.mark.asyncio
async def test_a_landed_follower_leaves_the_table():
    live = _live(merged_into="w-primary")
    settle.mark_unpersisted(live, S.FAILED, None)
    svc, released = _service(_Store(), live)
    assert await settle.persist_pending(svc) == 1
    assert svc._live == {} and released == []


@pytest.mark.asyncio
@pytest.mark.parametrize("current", ["terminal", "moved", "gone", "unreadable"])
async def test_a_stale_write_rereads_the_record(current):
    live = _live()
    settle.mark_unpersisted(live, S.FAILED, ReasonCode.RUNTIME_ERROR)

    def stale(work, state):
        raise StaleRevision(1, 2)

    def get(work_id):
        if current == "unreadable":
            raise OSError("locked")
        return {
            "terminal": _Work(work_id, S.CANCELLED),
            "moved": _Work(work_id, S.CANCELLING),
            "gone": None,
        }[current]

    svc, released = _service(_Store(advance=stale, get=get), live)
    landed = await settle.persist_pending(svc)
    if current == "terminal":
        assert landed == 1 and live.work.state is S.CANCELLED and released == ["w1"]
        return
    assert landed == 0 and released == [] and live.unpersisted is not None
    if current == "moved":
        assert live.unpersisted[0].state is S.CANCELLING


@pytest.mark.asyncio
async def test_a_write_that_still_fails_is_kept():
    live = _live()
    settle.mark_unpersisted(live, S.FAILED, None)

    def locked(work, state):
        raise RuntimeError("database is locked")

    svc, released = _service(_Store(advance=locked), live)
    assert await settle.persist_pending(svc) == 0
    assert live.unpersisted is not None and released == []


@pytest.mark.asyncio
async def test_an_invalid_transition_is_reread_too():
    live = _live()
    settle.mark_unpersisted(live, S.FAILED, None)

    def illegal(work, state):
        raise InvalidTransition(work.state, state)

    svc, _ = _service(_Store(advance=illegal, get=lambda wid: _Work(wid, S.FAILED)), live)
    assert await settle.persist_pending(svc) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_answer", ["raises", "running", "ended", "no-probe"])
async def test_confirm_stopped_waits_for_the_child(probe_answer):
    live = _live(S.FAILED, stopping_child="run-1")
    skipped = _live(S.FAILED)

    def probe(exec_id):
        if probe_answer == "raises":
            raise OSError("probe gone")
        return None if probe_answer == "running" else S.CANCELLED

    svc, released = _service(
        _Store(), live, skipped, probe=None if probe_answer == "no-probe" else probe
    )
    count = await settle.confirm_stopped(svc)
    if probe_answer in ("ended", "no-probe"):
        assert count == 1 and live.stopping_child == "" and released == ["w1"]
    else:
        assert count == 0 and live.stopping_child == "run-1" and released == []

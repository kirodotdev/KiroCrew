"""``real_dashboard_state`` settles a state's notification writes before teardown.

A ``notify`` made inside a running loop queues its ``notifications.jsonl`` append on
the ``notif-io`` executor, and the append resolves the data home when it runs. The
helper's settle scope must not end while such an append is still queued, or it lands
in the next test's home.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Iterator

import pytest
import real_dashboard_state as rds
from real_dashboard_state import _settle_real_dashboard_states  # noqa: F401
from real_dashboard_state import real_dashboard_state, settling_notification_writes

from kiro_crew.dashboard.state_parts.notification_log import (
    _notification_io_executor,
    _notifications_path,
)

GATE_WAIT_S = 30.0


class _GateReleasingExecutor:
    """The real executor, except a submit through it first opens *gate*.

    Only the settle scope submits through the helper's executor binding, so the
    worker parked on *gate* is released exactly when, and only if, teardown asks
    to wait for it.
    """

    def __init__(self, gate: threading.Event) -> None:
        self._gate = gate

    def submit(self, fn, /, *args, **kwargs):
        self._gate.set()
        return _notification_io_executor().submit(fn, *args, **kwargs)


@contextlib.contextmanager
def _parked_notification_worker(monkeypatch) -> Iterator[None]:
    """Hold the executor's one worker until the settle scope submits its sentinel."""
    gate = threading.Event()
    blocker = _notification_io_executor().submit(gate.wait, GATE_WAIT_S)
    monkeypatch.setattr(rds, "_notification_io_executor", lambda: _GateReleasingExecutor(gate))
    try:
        yield
    finally:
        gate.set()
        blocker.result(timeout=GATE_WAIT_S)


def _persisted_titles() -> list[str]:
    path = _notifications_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["title"] for line in lines if line.strip()]


def _notify_in_a_loop(state, title: str) -> None:
    async def _notify() -> None:
        state.notify("info", title, "body")
        assert state.last_notification_persist is not None

    asyncio.run(_notify())


def test_the_settle_scope_does_not_end_while_a_write_is_queued(tmp_path, monkeypatch):
    with _parked_notification_worker(monkeypatch):
        with settling_notification_writes():
            _notify_in_a_loop(real_dashboard_state(tmp_path), "queued")
            assert _persisted_titles() == []
        assert _persisted_titles() == ["queued"]


def test_a_scope_left_by_an_exception_still_waits_for_its_write(tmp_path, monkeypatch):
    with _parked_notification_worker(monkeypatch):
        with pytest.raises(RuntimeError, match="test body failed"):
            with settling_notification_writes():
                _notify_in_a_loop(real_dashboard_state(tmp_path), "raised")
                raise RuntimeError("test body failed")
        assert _persisted_titles() == ["raised"]


def test_the_settle_scope_ends_once_the_queued_write_lands(tmp_path):
    with settling_notification_writes():
        _notify_in_a_loop(real_dashboard_state(tmp_path), "landed")
    assert _persisted_titles() == ["landed"]


def test_a_synchronous_write_leaves_nothing_to_wait_for(tmp_path):
    with settling_notification_writes():
        state = real_dashboard_state(tmp_path)
        state.notify("info", "inline", "body")
        assert state.last_notification_persist is None
    assert _persisted_titles() == ["inline"]


def test_a_state_is_refused_outside_a_settle_scope(tmp_path, monkeypatch):
    monkeypatch.setitem(rds._settle, "active", False)
    with pytest.raises(RuntimeError, match="_settle_real_dashboard_states"):
        real_dashboard_state(tmp_path)

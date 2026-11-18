"""The dashboard wiring of the host memory guard (rfc-host-memory-guard.md §4.4).

The notifier's trigger reaches ``session_memory_guard.request``; a stop posts one
notice in the chat that started the tree and, with a ledger summary, files the
guard report under the worker's ledger key; a key with no dashboard tab posts
nothing and still files the report.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import session_memory_guard
from kiro_crew.dashboard import memory_guard_report
from kiro_crew.dashboard.memory_guard_events import wire_memory_guard


class Slot:
    def __init__(self, key: str) -> None:
        self.key = key
        self.rows: list[tuple[str, str, dict[str, Any]]] = []
        self.cuts: list[bool] = []

    def _steer_segment_cut(self, *, interrupted: bool) -> None:
        self.cuts.append(interrupted)

    def append(self, role: str, text: str, cls: str, *, meta: dict[str, Any]) -> None:
        self.rows.append((role, text, meta))


def _state(slots: dict[str, Slot]) -> SimpleNamespace:
    frames: list[tuple[str, dict[str, Any]]] = []
    return SimpleNamespace(
        sessions=SimpleNamespace(),
        resource_pressure_notifier=SimpleNamespace(memory_guard=None),
        get_slot=slots.get,
        broadcast_ws=lambda kind, data: frames.append((kind, data)),
        frames=frames,
    )


@pytest.fixture
def filed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def record(state: Any, worker_key: str, summary: str) -> str:
        calls.append((worker_key, summary))
        return "it_1"

    monkeypatch.setattr(memory_guard_report, "record_guard_report", record)
    return calls


def test_the_notifier_trigger_reaches_the_guard_request(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state({})
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(session_memory_guard, "request", lambda *args: seen.append(args))
    wire_memory_guard(state)
    state.resource_pressure_notifier.memory_guard("status", 12.0, "psi")
    assert seen == [(state.sessions, "status", 12.0, "psi")]


def test_a_stop_posts_one_notice_and_files_the_report(filed: list[tuple[str, str]]) -> None:
    slot = Slot("chat-2-worker")
    state = _state({"chat-2-worker": slot})
    wire_memory_guard(state)

    async def scenario() -> None:
        state.sessions.on_memory_guard_stop("dashboard:chat-2-worker", "stopped it", "summary")
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert slot.rows == [
        ("assistant", "stopped it", {"kind": "compaction", "notice": "memory_guard"})
    ]
    assert slot.cuts == [False]
    assert state.frames == [("chat_segment", {"slot": "chat-2-worker"})]
    assert filed == [("chat-2-worker", "summary")]


def test_no_summary_files_no_report(filed: list[tuple[str, str]]) -> None:
    slot = Slot("chat-2-worker")
    state = _state({"chat-2-worker": slot})
    wire_memory_guard(state)

    async def scenario() -> None:
        state.sessions.on_memory_guard_stop("dashboard:chat-2-worker", "could not stop", None)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert [text for _, text, _ in slot.rows] == ["could not stop"]
    assert filed == []


def test_a_key_with_no_tab_posts_nothing_and_still_files(filed: list[tuple[str, str]]) -> None:
    state = _state({})
    wire_memory_guard(state)

    async def scenario() -> None:
        state.sessions.on_memory_guard_stop("chat-9-unsurfaced", "stopped it", "summary")
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert filed == [("chat-9-unsurfaced", "summary")]


def test_a_failing_append_is_logged_and_the_report_still_files(
    filed: list[tuple[str, str]], caplog: pytest.LogCaptureFixture
) -> None:
    class Broken(Slot):
        def append(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("transcript closed")

    state = _state({"chat-2-worker": Broken("chat-2-worker")})
    wire_memory_guard(state)

    async def scenario() -> None:
        state.sessions.on_memory_guard_stop("dashboard:chat-2-worker", "stopped it", "summary")
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert "Failed to append memory-guard notice" in caplog.text
    assert filed == [("chat-2-worker", "summary")]

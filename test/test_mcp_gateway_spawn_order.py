"""The order the gateway daemon's spawn gate serves waiting starts in.

A spawn a person's session start is waiting on goes ahead of background ones, a
background one is passed a bounded number of times, and a cut to the floor changes
the width and not the order. The priority is the session start's own: written onto
a person's stub entries, reported once by the stub, and never part of the pool key.
The gate's other contracts, and the daemon's side of the wire, are pinned in
``test_mcp_gateway_spawn_gate.py``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from kiro_crew.mcp_gateway import admission as adm
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway import stub_register
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.session_servers import attach_stub_start_priority
from kiro_crew.start_priority import START_PRIORITY_ENV, StartPriority, priority_from_wire

_FG = StartPriority.FOREGROUND
_BG = StartPriority.BACKGROUND


async def _settle() -> None:
    """Let every ready callback run (a few loop turns)."""
    for _ in range(5):
        await asyncio.sleep(0)


class _Queue:
    """Starts queued behind a gate, and the order the gate serves them in."""

    def __init__(self, gate: adm.SpawnGate) -> None:
        self.gate = gate
        self.served: list[str] = []
        self.tasks: dict[str, asyncio.Task[adm.Permit]] = {}

    async def add(self, label: str, priority: StartPriority | None = None) -> None:
        async def wait() -> adm.Permit:
            kwargs = {} if priority is None else {"priority": priority}
            permit = await self.gate.acquire(label=label, **kwargs)
            self.served.append(label)
            return permit

        self.tasks[label] = asyncio.create_task(wait())
        await _settle()

    async def serve_all(self) -> list[str]:
        """Release each served start in turn until the queue is empty."""
        done: set[str] = set()
        while len(done) < len(self.tasks):
            await _settle()
            label = next(name for name in self.served if name not in done)
            (await self.tasks[label]).release()
            done.add(label)
        return self.served


class TestStartOrder:
    @pytest.mark.asyncio
    async def test_a_person_waiting_goes_ahead_of_background_spawns(self) -> None:
        gate = adm.SpawnGate(1)
        held = await gate.acquire(label="held")
        queue = _Queue(gate)
        await queue.add("bg-1", _BG)
        await queue.add("bg-2", _BG)
        await queue.add("person", _FG)
        assert gate.queued == 3

        held.release()

        assert await queue.serve_all() == ["person", "bg-1", "bg-2"]
        assert gate.in_flight == 0 and gate.queued == 0

    @pytest.mark.asyncio
    async def test_a_background_spawn_is_passed_at_most_four_times(self) -> None:
        gate = adm.SpawnGate(1)
        held = await gate.acquire(label="held")
        queue = _Queue(gate)
        await queue.add("bg", _BG)
        for i in range(6):
            await queue.add(f"fg-{i}", _FG)

        held.release()

        assert await queue.serve_all() == ["fg-0", "fg-1", "fg-2", "fg-3", "bg", "fg-4", "fg-5"]

    @pytest.mark.asyncio
    async def test_a_cut_to_the_floor_changes_the_width_not_the_order(self) -> None:
        gate = adm.SpawnGate(2, floor=1, ceiling=4)
        holders = [await gate.acquire(label=f"held-{i}") for i in range(2)]
        queue = _Queue(gate)
        await queue.add("bg-1", _BG)
        await queue.add("fg-1", _FG)
        await queue.add("bg-2", _BG)
        await queue.add("fg-2", _FG)

        # The controller's pause: the gate drops to its floor and stays there.
        assert gate.set_capacity(0) == 1
        holders[0].release()
        await _settle()
        assert queue.served == []
        holders[1].release()
        await _settle()
        assert queue.served == ["fg-1"]
        assert (gate.capacity, gate.in_flight, gate.queued) == (1, 1, 3)

        # The resume: the rest are admitted in the same order.
        gate.set_capacity(4)
        await _settle()
        assert queue.served == ["fg-1", "fg-2", "bg-1", "bg-2"]

    @pytest.mark.asyncio
    async def test_a_caller_that_names_no_priority_is_background(self) -> None:
        gate = adm.SpawnGate(1)
        held = await gate.acquire(label="held")
        queue = _Queue(gate)
        await queue.add("old-1")
        await queue.add("old-2")
        await queue.add("person", _FG)

        held.release()

        assert await queue.serve_all() == ["person", "old-1", "old-2"]


class TestStartPriorityOnTheWire:
    @pytest.mark.parametrize(
        ("value", "priority"),
        [("fg", _FG), ("bg", _BG), (None, _BG), ("FG", _BG), (1, _BG), ("", _BG)],
    )
    def test_only_the_exact_claim_is_foreground(self, value: Any, priority: StartPriority) -> None:
        assert priority_from_wire(value) is priority

    @pytest.mark.parametrize(
        ("env", "field"), [("fg", "fg"), ("bg", None), (None, None)], ids=["person", "bg", "unset"]
    )
    def test_a_stub_reports_the_priority_once_and_keeps_it_from_what_it_execs(
        self, monkeypatch, env: str | None, field: str | None
    ) -> None:
        if env is None:
            monkeypatch.delenv(START_PRIORITY_ENV, raising=False)
        else:
            monkeypatch.setenv(START_PRIORITY_ENV, env)
        args = stub_mod._parse_args(
            ["--server", "echo-mcp", "--agent", "adm-agent"]
            + ["--target-command", "/bin/true", "--work-dir", "/tmp"]
        )

        payload = stub_register.build_register_payload(args)

        # An old daemon never sees a field it does not know for a background stub.
        assert payload.get("start_priority") == field
        assert ("start_priority" in payload) is (field is not None)
        # Read once and removed: a target the stub execs itself does not inherit it.
        assert START_PRIORITY_ENV not in os.environ
        # Order at the gate, never pool identity.
        assert PoolKey.from_register(payload) == PoolKey.from_register(
            {**payload, "start_priority": "fg"}
        )

    @pytest.mark.parametrize("priority", [_FG, _BG], ids=["person", "background"])
    def test_only_a_persons_session_start_is_written_onto_its_stub_entries(
        self, priority: StartPriority
    ) -> None:
        entries = [
            {"name": "demo", "command": "kirocrew", "env": [{"name": "A", "value": "1"}]},
            {"name": "bare", "command": "kirocrew"},
        ]

        stamped = attach_stub_start_priority(entries, priority)

        if priority is _BG:
            assert stamped is entries
            return
        pair = {"name": START_PRIORITY_ENV, "value": "fg"}
        assert [e["env"] for e in stamped] == [[{"name": "A", "value": "1"}, pair], [pair]]
        # The caller's list may be shared with another session: it is not edited.
        assert "env" not in entries[1] and len(entries[0]["env"]) == 1

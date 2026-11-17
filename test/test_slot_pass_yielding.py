"""The Crew Mode routes' slot pass lets the event loop run.

With a few hundred open sessions one synchronous ``serialize_slots`` pass holds
the loop past the desktop stall budget, and the alarm restarts the gateway.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import crew_workers
from kiro_crew.dashboard.state_parts import slots_frame

#: Each row costs this much: 300 of them hold a synchronous pass for 1.5 s.
ROW_COST_S = 0.005
SLOTS = 300


def _state(rows: dict[str, dict], seen: list[int] | None = None, beats: list[int] | None = None):
    def serialize_slot(slot, **_k):
        time.sleep(ROW_COST_S)
        if seen is not None and beats is not None:
            seen.append(beats[0])
        return dict(rows[slot.key])

    state = SimpleNamespace(
        _slots={key: SimpleNamespace(key=key) for key in rows},
        subagents=None,
        _slots_under_construction=(),
        _drop_orphaned_mcp_report=lambda slot: None,
        serialize_slot=serialize_slot,
    )
    state.serialize_slots_yielding = lambda **k: slots_frame.serialize_slots_yielding(state, **k)
    return state


def _rows() -> dict[str, dict]:
    rows = {"root": {"key": "root", "agent": "kirocrew-conductor", "agent_kind": "template"}}
    for n in range(SLOTS - 1):
        rows[f"w{n}"] = {
            "key": f"w{n}",
            "agent": "kirocrew-worker",
            "agent_kind": "template",
            "created_by": "root",
        }
    return rows


@pytest.mark.asyncio
async def test_the_crew_routes_pass_keeps_the_loop_answering_with_many_sessions():
    """Another task runs while the rows are built: the pass yields mid-list."""
    beats = [0]
    seen: list[int] = []
    state = _state(_rows(), seen, beats)
    done = asyncio.Event()

    async def heartbeat() -> None:
        while not done.is_set():
            await asyncio.sleep(0)
            beats[0] += 1

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    rows = await crew_workers._stamped_rows(state)
    done.set()
    await beat

    assert len(rows) == SLOTS
    # The rows take 1.5 s at 20 ms slices: the other task ran between many of them.
    assert len(set(seen)) > SLOTS // 10, sorted(set(seen))
    by_key = {row["key"]: row for row in rows}
    assert by_key["root"]["crew_mode_root"] is True
    assert by_key["w0"]["crew_root"] == "root"


@pytest.mark.asyncio
async def test_a_yielding_pass_matches_the_synchronous_one_and_skips_a_slot_closed_meanwhile():
    rows = _rows()
    state = _state(rows)
    want = [row["key"] for row in slots_frame.serialize_slots(state)]  # type: ignore[arg-type]

    async def close_late() -> None:
        while len(state._slots) == SLOTS:
            await asyncio.sleep(0)
            state._slots.pop(f"w{SLOTS - 2}", None)

    closer = asyncio.create_task(close_late())
    got = [row["key"] for row in await slots_frame.serialize_slots_yielding(state)]  # type: ignore[arg-type]
    await closer
    assert got == [key for key in want if key != f"w{SLOTS - 2}"]


@pytest.mark.asyncio
async def test_a_worker_tab_closed_during_the_pass_is_refused(monkeypatch):
    """The tab check runs after the pass's awaits, so a steer never reaches a closed tab."""
    worker = SimpleNamespace(key="w0")
    slots = {"w0": worker}

    async def pass_that_closes_the_tab(**_k):
        slots.pop("w0")
        return [{"key": "w0", "crew_root": "root"}]

    async def loaded(_slot):
        return None

    state = SimpleNamespace(get_slot=slots.get, serialize_slots_yielding=pass_that_closes_the_tab)
    monkeypatch.setattr(crew_workers, "_load_window", loaded)
    monkeypatch.setattr(crew_workers, "_tree_workers", lambda rows, _root: rows)
    refused = await crew_workers._load_worker_window(state, "root", "w0", worker)
    assert refused is not None and refused.status == 409

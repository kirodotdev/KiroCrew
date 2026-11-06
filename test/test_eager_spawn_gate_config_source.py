"""Where ``schedule_eager_spawn`` reads ``session.eager_spawn`` from.

The gate runs on the event loop and reads only the live-config watcher's snapshot:
armed, the snapshot answers whatever ``degraded_sections`` says; unprimed, no spawn
and no disk read. Each case sets the disk load to the opposite answer and asserts it
was never called.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import schedule_eager_spawn
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


@pytest.fixture(autouse=True)
def _isolate_armed_registry():
    """A spawned task registers in a module global; keep tests independent."""
    chat_runner._armed_prefetches.clear()
    yield
    chat_runner._armed_prefetches.clear()


def _cfg(enabled: bool, *, degraded: frozenset[str] = frozenset()) -> KiroCrewConfig:
    cfg = KiroCrewConfig(agents={"default": KiroCrewAgentConfig()}, _degraded_sections=degraded)
    cfg.session.eager_spawn = enabled
    return cfg


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state.get_slot = MagicMock(return_value=slot)
    state.sessions = MagicMock()
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    state.sessions.release = MagicMock()
    state.sessions.resumable_hint = MagicMock(return_value=True)
    return state


def _schedule(*, snapshot: KiroCrewConfig | None, on_disk):
    """Run the gate with *snapshot* armed and *on_disk* behind the disk load.

    *snapshot* goes through :meth:`ConfigWatch.prime`, as at boot; ``None`` leaves the
    watcher unbuilt. Returns ``(task, load_mock)``; the task is cancelled before its
    first step, since only the read is under test.
    """
    if snapshot is not None:
        live.watch().prime(snapshot)
    load = MagicMock(**on_disk)
    slot = _ChatSlot("t1")
    state = _mock_state(slot)
    with patch.object(chat_runner.KiroCrewConfig, "load", load):
        task = schedule_eager_spawn(state, slot)
    if task is not None:
        task.cancel()
    return task, load


@pytest.mark.asyncio
async def test_armed_snapshot_answers_without_touching_disk():
    """The adopted snapshot decides, and the disk load never runs.

    Both directions, because a gate that ignores the snapshot would agree with
    it by luck in one of them: the disk copy is pinned to the OPPOSITE value,
    so whichever way the snapshot points, only a snapshot read gets it right.
    """
    task, load = _schedule(snapshot=_cfg(False), on_disk={"return_value": _cfg(True)})
    assert task is None
    assert load.call_count == 0

    task, load = _schedule(snapshot=_cfg(True), on_disk={"return_value": _cfg(False)})
    assert task is not None
    assert load.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("degraded", [DEGRADED_WHOLE_CONFIG, "session"])
async def test_degraded_flagged_snapshot_is_still_trusted(degraded):
    """A degradation marker does not send the gate back to the disk.

    The marker is sticky, and on a torn file the watcher keeps the previous values,
    while a fresh load reads the default ON. So the snapshot says OFF, the disk ON.
    """
    task, load = _schedule(
        snapshot=_cfg(False, degraded=frozenset({degraded})),
        on_disk={"return_value": _cfg(True)},
    )
    assert task is None
    assert load.call_count == 0


@pytest.mark.asyncio
async def test_unprimed_snapshot_skips_the_spawn_without_reading_the_file():
    """No snapshot means no spawn, and specifically NOT a fallback disk read.

    The watcher is primed only after the listener binds, and a disk parse on the loop
    is the stall this gate removes. The disk says ON; skipping costs only a cold start.
    """
    task, load = _schedule(snapshot=None, on_disk={"return_value": _cfg(True)})
    assert task is None
    assert load.call_count == 0

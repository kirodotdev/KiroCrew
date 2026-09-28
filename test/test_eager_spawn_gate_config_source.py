"""Where ``schedule_eager_spawn`` reads ``session.eager_spawn`` from.

The gate runs on the event loop, from four slot-signal handlers, and needs one
boolean. It reads the live-config watcher's already-adopted snapshot, which is a
plain attribute read, and there is no disk load behind it at all.

Every case of that choice has a verdict here:

* snapshot armed -> the snapshot answers, whatever its ``degraded_sections``
  reports;
* snapshot unprimed -> no spawn, and still no disk read, because this runs on the
  loop and the watcher is primed only after the listener binds.

Each case pins the disk load to the OPPOSITE answer and asserts it was never
called, so a gate that reached for the file would fail rather than pass by luck.
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

    *snapshot* is adopted through :meth:`ConfigWatch.prime`, the same call boot
    makes, so the gate reads it back through the real accessor rather than a
    stub of it. ``None`` leaves the process watcher unbuilt, which is the state
    the autouse ``_drop_live_config_snapshot`` fixture guarantees.

    Returns ``(task, load_mock)``. The task is cancelled before it can take its
    first step: what is under test is where the gate read its boolean, not the
    spawn that follows.
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

    Two reasons, and they point the same way. The marker is sticky for the life
    of the process, so keying on it would put every later slot signal back on the
    synchronous load. And while a document does not parse, the watcher keeps the
    PREVIOUS adopted values and takes only the marker from the torn load, so the
    snapshot is the copy holding the operator's real setting while a fresh load
    of that torn file reads the default, which is ON.

    So the snapshot carries OFF here, the disk copy carries the default ON, and
    a gate that distrusted a marked snapshot would spawn for someone who turned
    the feature off.
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

    The gate runs on the event loop and the watcher is primed only after the
    listener binds, so a request arriving in that window would pay a whole-file
    config parse on the loop: the very stall this gate exists to remove. The
    disk copy says ON, and a gate that consulted it would arm a task here.

    Skipping costs a cold start on the slot's first turn, which is what
    speculative pre-warming is allowed to lose.
    """
    task, load = _schedule(snapshot=None, on_disk={"return_value": _cfg(True)})
    assert task is None
    assert load.call_count == 0

"""A boot's orphan tombstone is written only once the orphan's notice is recorded.

The tombstone excludes a run folder from every later reconciliation, so a tombstone
written before the notice exists turns a crash in between into an announcement no one
ever receives. The reconciler runs for real on a tmp_path home; its notifiers are
recorders and ``_is_pid_alive`` says dead, so nothing is signalled or sent. No
cleanup, sweep, prune or reap function is called.

The gateway builds its manager with ``hold_orphan_notices``: the sweep ends each
orphan's process at once and holds the notices until the dashboard has restored the
tabs they go to. The last tests here pin that hold, its bound, and a cancelled hold.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import subagent_persistence as sp
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager import monitoring
from kiro_crew.testing.wait import await_until, until_parked

PARENT = "dashboard:chat-7"


@pytest.fixture(autouse=True)
def _pinned_paths(tmp_path, _floor_monkeypatch):
    """Pin the data home and workspace under ``tmp_path`` for every test here.

    Through ``_floor_monkeypatch``, never the shared ``monkeypatch``: a test's
    ``monkeypatch.undo()`` reverts every record on that instance, and would lift
    these pins with it.
    """
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        path = tmp_path / sub
        path.mkdir()
        _floor_monkeypatch.setenv(var, str(path))


@pytest.fixture(autouse=True)
def _managers(close_subagent_managers):
    """Managers built here are closed at teardown (``conftest``)."""


class _ProcessDied(BaseException):
    """Stands for the gateway process ending at this point (not an Exception)."""


def _finished_run(agent_id: str) -> None:
    sp.create_agent_folder(agent_id, task="summarise the logs", parent_session=PARENT)
    assert sp.write_finished_result(agent_id, "the whole answer", sp.update_state)


def _boot(monkeypatch, *, injects: bool = True) -> tuple[SubagentManager, list[str]]:
    """A fresh gateway whose notifiers record what they are handed."""
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), on_done=AsyncMock())
    sent: list[str] = []

    async def _inject(parent_session, msg, meta=None, delivery=None):  # type: ignore[no-untyped-def]
        if injects:
            sent.append(parent_session)
        return injects

    async def _dm(msg):  # type: ignore[no-untyped-def]
        sent.append("owner-dm")

    monkeypatch.setattr(manager, "_try_inject_orphan_notification", _inject)
    monkeypatch.setattr(manager, "_send_orphan_slack_dm", _dm)
    monkeypatch.setattr(manager, "_is_pid_alive", lambda _pid: False)
    return manager, sent


async def _next_boot(monkeypatch) -> list[str]:
    manager, sent = _boot(monkeypatch)
    await manager._reconcile_orphans()
    return sent


@pytest.mark.asyncio
async def test_a_death_before_the_notice_leaves_the_run_for_the_next_boot(monkeypatch):
    _finished_run("orp0001")
    first, _sent = _boot(monkeypatch)

    async def _dies(agent_id, state, has_result):  # type: ignore[no-untyped-def]
        raise _ProcessDied()

    monkeypatch.setattr(first, "_notify_orphan", _dies)
    with pytest.raises(_ProcessDied):
        await first._reconcile_orphans()
    assert await _next_boot(monkeypatch) == [
        PARENT
    ], "a finished run whose notice was never created is announced to no one at the next boot"


@pytest.mark.asyncio
async def test_a_death_before_the_digest_dm_leaves_the_run_for_the_next_boot(monkeypatch):
    _finished_run("orp0002")
    first, _sent = _boot(monkeypatch, injects=False)  # no open tab: the owner DM path

    async def _dies(msg):  # type: ignore[no-untyped-def]
        raise _ProcessDied()

    monkeypatch.setattr(first, "_send_orphan_slack_dm", _dies)
    with pytest.raises(_ProcessDied):
        await first._reconcile_orphans()
    assert await _next_boot(monkeypatch) == [PARENT], (
        "a run bound for the owner DM was tombstoned before the DM was sent and is announced "
        "to no one"
    )


@pytest.mark.asyncio
async def test_a_normal_boot_announces_each_orphan_exactly_once(monkeypatch):
    _finished_run("orp0003")
    _finished_run("orp0004")
    assert await _next_boot(monkeypatch) == [PARENT, PARENT]
    assert await _next_boot(monkeypatch) == []
    tomb = sp.read_tombstone("orp0003")
    assert (tomb["cause"], tomb["recovery_action"], tomb.get("outcome")) == (
        "gateway_restart",
        "delivered",
        "completed",
    )


@pytest.mark.asyncio
async def test_a_dm_bound_orphan_is_announced_once_and_tombstoned_after_the_dm(monkeypatch):
    _finished_run("orp0005")
    first, sent = _boot(monkeypatch, injects=False)
    await first._reconcile_orphans()
    assert sent == ["owner-dm"]
    tomb = sp.read_tombstone("orp0005")
    assert (tomb["cause"], tomb["recovery_action"], tomb.get("outcome")) == (
        "gateway_restart",
        "result_available",
        "completed",
    )
    assert await _next_boot(monkeypatch) == []


@pytest.mark.asyncio
async def test_a_run_delivered_before_the_restart_is_not_announced(monkeypatch):
    _finished_run("orp0006")
    sp.mark_delivered("orp0006", elapsed=1.0, credits=0.0)
    assert await _next_boot(monkeypatch) == []


@pytest.mark.asyncio
async def test_a_digest_dm_that_raises_still_tombstones_the_run(monkeypatch):
    # A broken DM channel raises at every start, so it must not make the run
    # re-announce at every boot.
    _finished_run("orp0007")
    first, _sent = _boot(monkeypatch, injects=False)

    async def _raises(msg):  # type: ignore[no-untyped-def]
        raise RuntimeError("DM channel down")

    monkeypatch.setattr(first, "_send_orphan_slack_dm", _raises)
    await first._reconcile_orphans()
    assert await _next_boot(monkeypatch) == []


@pytest.mark.asyncio
async def test_a_notice_taken_without_a_tombstone_still_ends_tombstoned(monkeypatch):
    # The notifier reports the notice taken but wrote no tombstone of its own:
    # the boot's own is written, as before.
    _finished_run("orp0008")
    first, _sent = _boot(monkeypatch)
    monkeypatch.setattr(first, "_notify_orphan", AsyncMock(return_value=None))
    await first._reconcile_orphans()
    assert sp.read_tombstone("orp0008")["recovery_action"] == "result_available"
    assert await _next_boot(monkeypatch) == []


class _HeldBoot:
    """A gateway-built manager: notices held for the release, the gateway's two seams
    recorded. ``tab_open`` is what the gateway's ``on_orphan_notify`` answers: False is
    a dashboard that has not restored the parent's tab."""

    def __init__(self, monkeypatch, *, tab_open: bool = True) -> None:
        self.ended: list[str] = []
        self.tab: list[str] = []
        self.owner_dm: list[str] = []

        async def _notify(parent_session, msg, meta=None, delivery=None):  # type: ignore[no-untyped-def]
            if tab_open:
                self.tab.append(parent_session)
            return tab_open

        async def _dm(msg):  # type: ignore[no-untyped-def]
            self.owner_dm.append(msg)
            return True

        async def _end(agent_id, state, *, reason):  # type: ignore[no-untyped-def]
            self.ended.append(agent_id)
            return "killed"

        self.manager = SubagentManager(
            sessions=MagicMock(),
            ctx_builder=MagicMock(),
            on_done=AsyncMock(),
            on_orphan_notify=_notify,
            on_orphan_dm=_dm,
            hold_orphan_notices=True,
        )
        monkeypatch.setattr(self.manager, "_end_orphan_process", _end)
        monkeypatch.setattr(self.manager, "_is_pid_alive", lambda _pid: False)
        monkeypatch.setattr("kiro_crew.subagent.sel", MagicMock())

    async def parked_sweep(self) -> asyncio.Task[None]:
        """Start the boot sweep and return it once it waits for the release.

        Raced against the sweep itself, so a sweep that sends its notices without
        waiting ends first and the caller's assertions name it, instead of the barrier
        running to its lost-run bound.
        """
        sweep = asyncio.create_task(self.manager._reconcile_orphans())
        parked = asyncio.create_task(until_parked(self.manager._orphan_notice_gate))
        await asyncio.wait({sweep, parked}, return_when=asyncio.FIRST_COMPLETED)
        if not parked.done():
            parked.cancel()
        return sweep


def _running_orphan(agent_id: str) -> None:
    """A finished run whose record still names its process."""
    _finished_run(agent_id)
    assert sp.update_state(agent_id, pid=4242)


@pytest.mark.asyncio
async def test_a_held_boot_ends_the_process_at_once_and_notifies_only_after_the_release(
    monkeypatch,
):
    _running_orphan("orp0009")
    boot = _HeldBoot(monkeypatch)
    sweep = await boot.parked_sweep()
    assert not sweep.done(), "the sweep sent its notices before the gateway released them"
    assert boot.ended == ["orp0009"], "the orphan's process waited for the notice hold"
    assert (boot.tab, boot.owner_dm) == ([], [])
    assert sp.read_tombstone("orp0009") is None

    boot.manager.release_orphan_notices()
    boot.manager.release_orphan_notices()  # idempotent: the gateway's finally may repeat it
    await await_until(sweep, what="the released sweep")

    assert boot.tab == [PARENT]
    assert boot.owner_dm == [], "a notice the restored tab took was also sent to the owner DM"
    assert sp.read_tombstone("orp0009")["recovery_action"] == "delivered"


@pytest.mark.asyncio
async def test_a_hold_never_released_still_sends_the_owner_dm_digest(monkeypatch, caplog):
    # The dashboard never came up: no release, and no tab to inject into.
    _running_orphan("orp0010")
    monkeypatch.setattr(monitoring, "_ORPHAN_NOTICE_HOLD_SECS", 0.01)
    boot = _HeldBoot(monkeypatch, tab_open=False)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
        sweep = asyncio.create_task(boot.manager._reconcile_orphans())
        await await_until(sweep, what="the sweep whose hold runs out")

    assert any("with no release from the gateway" in r.getMessage() for r in caplog.records)
    assert len(boot.owner_dm) == 1 and "orp0010" in boot.owner_dm[0]
    assert sp.read_tombstone("orp0010")["recovery_action"] == "result_available"
    assert await _next_boot(monkeypatch) == []


@pytest.mark.asyncio
async def test_a_sweep_cancelled_in_the_hold_leaves_the_run_for_the_next_boot(monkeypatch):
    # The gateway stopped during its own boot, while the notices were held.
    _running_orphan("orp0011")
    boot = _HeldBoot(monkeypatch)
    sweep = await boot.parked_sweep()
    assert not sweep.done(), "the sweep sent its notices before the gateway released them"
    sweep.cancel()
    await asyncio.gather(sweep, return_exceptions=True)

    assert sp.read_tombstone("orp0011") is None
    assert await _next_boot(monkeypatch) == [PARENT]

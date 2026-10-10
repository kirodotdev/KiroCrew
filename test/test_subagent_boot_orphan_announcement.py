"""A boot's orphan tombstone is written only once the orphan's notice is recorded.

The tombstone excludes a run folder from every later reconciliation, so a tombstone
written before the notice exists turns a crash in between into an announcement no one
ever receives. The reconciler runs for real on a tmp_path home; its notifiers are
recorders and ``_is_pid_alive`` says dead, so nothing is signalled or sent. No
cleanup, sweep, prune or reap function is called.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import subagent_persistence as sp
from kiro_crew.subagent import SubagentManager

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

    async def _inject(parent_session, msg, meta=None):  # type: ignore[no-untyped-def]
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

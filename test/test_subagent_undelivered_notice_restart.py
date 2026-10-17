"""A report the gateway gave up on stays recoverable until its notice is drained.

When every injection attempt fails, ``notify_injection_failed`` marks the record
``_report_undelivered`` and the gateway queues a notice for the parent agent in
``slot._pending_subagent_failures``, which lives only in memory. The run loop
therefore leaves the folder without a ``delivered`` tombstone, so a restart
before the parent's next turn hands the run to the next start's orphan
reconciliation. The drain that gives the notice to the parent settles the
tombstone (``settle_drained_subagent_notices``), so a run whose notice was read
is not announced again.

The run loop's terminal report and the next start's ``_reconcile_orphans`` run
for real on a tmp_path home. ``on_done`` stands for the gateway's
``_subagent_done``, and the next start's notifiers are recorders.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

PARENT = "dashboard:chat-7"

#: A ceiling that turns a hang into a failure that names itself. It never decides
#: whether a test passes, so it is far above anything these waits take.
_HANG_CEILING_SECS = 30.0


async def _settled(state) -> None:  # type: ignore[no-untyped-def]
    """Wait for the writer task a drain started on ``state``."""
    await asyncio.wait_for(asyncio.gather(*state._background_tasks), timeout=_HANG_CEILING_SECS)


@pytest.fixture(autouse=True)
def _pinned_paths(tmp_path, _floor_monkeypatch):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        path = tmp_path / sub
        path.mkdir()
        _floor_monkeypatch.setenv(var, str(path))


@pytest.fixture(autouse=True)
def _managers(close_subagent_managers):
    """Managers built here are closed at teardown (``conftest``)."""


def _finished_run(agent_id: str):  # type: ignore[no-untyped-def]
    from kiro_crew import subagent_persistence as sp
    from kiro_crew.subagent import SubagentInfo

    sp.create_agent_folder(agent_id, task="summarise the logs", parent_session=PARENT)
    assert sp.write_finished_result(agent_id, "the whole answer", sp.update_state)
    info = SubagentInfo(id=agent_id, task="summarise the logs")
    info.parent_session_key = PARENT
    info.done = True
    info.result_path = str(sp._agent_dir(agent_id) / "result.txt")
    return info


def _manager(**kwargs):  # type: ignore[no-untyped-def]
    from kiro_crew.subagent import SubagentManager

    kwargs.setdefault("on_done", AsyncMock())
    return SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), **kwargs)


async def _next_start_announcements(monkeypatch) -> list[str]:
    manager = _manager()
    sent: list[str] = []

    async def _inject(parent_session, msg, meta=None, delivery=None):  # type: ignore[no-untyped-def]
        sent.append(parent_session)
        return True

    async def _dm(msg):  # type: ignore[no-untyped-def]
        sent.append("owner-dm")

    monkeypatch.setattr(manager, "_try_inject_orphan_notification", _inject)
    monkeypatch.setattr(manager, "_send_orphan_slack_dm", _dm)
    monkeypatch.setattr(manager, "_is_pid_alive", lambda _pid: False)
    await manager._reconcile_orphans()
    return sent


async def _report(manager, info) -> None:  # type: ignore[no-untyped-def]
    """Report *info*'s end, then wait for the background work the report started.

    A notice that could not be delivered is queued for the parent agent by a task
    the report starts (the ``subagent_injection_failed`` event), so the wait is on
    that task, with a ceiling that only names a hang, rather than on a fixed number
    of event-loop turns.
    """
    before = asyncio.all_tasks()
    await manager._report_terminal(
        info,
        source="test",
        injection_timeout_reason="injection timed out",
        mark_delivered_on_success=True,
    )
    started = asyncio.all_tasks() - before
    if started:
        _done, pending = await asyncio.wait(started, timeout=_HANG_CEILING_SECS)
        assert not pending, f"the report's background work did not finish: {pending}"


def _giving_up_manager(queued: list[str]):  # type: ignore[no-untyped-def]
    async def _on_event(etype, ev_info, data=None):  # type: ignore[no-untyped-def]
        if etype == "subagent_injection_failed":
            queued.append((data or {}).get("failure_msg", ""))

    manager = _manager(on_event=_on_event)

    async def _gives_up(done_info):  # type: ignore[no-untyped-def]
        # gateway.py ``_subagent_done``: every attempt failed, so it announces the
        # failure and returns normally.
        manager.notify_injection_failed(done_info, reason="all 3 injection attempts failed")

    manager._on_done = _gives_up
    return manager


@pytest.mark.asyncio
async def test_a_restart_before_the_drain_announces_a_given_up_report_again(monkeypatch):
    from kiro_crew import subagent_persistence as sp

    info = _finished_run("injB001")
    queued: list[str] = []
    await _report(_giving_up_manager(queued), info)
    assert info._report_undelivered is True
    assert len(queued) == 1, "the failure notice was not queued for the parent agent"
    assert sp.read_tombstone("injB001") is None
    queued.clear()  # the restart: the slot and its queue end with the process
    assert await _next_start_announcements(monkeypatch) == [PARENT]


@pytest.mark.asyncio
async def test_a_drained_notice_settles_the_tombstone_and_is_not_announced_again(
    monkeypatch,
):
    from kiro_crew import subagent_persistence as sp
    from kiro_crew.dashboard.chat_runner import settle_drained_subagent_notices
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.subagent import SubagentDelivery

    info = _finished_run("injC001")
    queued: list[str] = []
    manager = _giving_up_manager(queued)
    await _report(manager, info)
    slot = _ChatSlot("chat-7")
    # What the gateway's ``subagent_injection_failed`` handler records.
    slot._pending_subagent_failures.append(queued[0])
    slot.note_pending_subagent_delivery(
        queued[0], [SubagentDelivery(info.id, info.elapsed, info.credits)]
    )
    state = MagicMock()
    state.subagents = manager
    state._background_tasks = set()
    failures = slot._pending_subagent_failures[:]
    slot._pending_subagent_failures.clear()
    settle_drained_subagent_notices(state, slot, failures)
    await _settled(state)
    tomb = sp.read_tombstone("injC001") or {}
    assert tomb.get("cause") == "delivered"
    assert not slot.owes_subagent_delivery(failures)
    assert await _next_start_announcements(monkeypatch) == []


@pytest.mark.asyncio
async def test_the_parent_turn_settles_the_notice_it_drains(monkeypatch, tmp_path):
    """The drain is wired into the parent's next turn.

    ``_run_chat`` runs for real on a ``DashboardState``; only the model is a
    stand-in, which records the message it is handed. The turn hands the queued
    notice to the model and settles the run's tombstone, so the next start does
    not announce the run again.
    """
    from chat_test_helpers import _make_state, drain_background_tasks

    from kiro_crew import subagent_persistence as sp
    from kiro_crew.context import ContextBuilder
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.memory import MemoryStore
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
    from kiro_crew.skills import SkillsLoader
    from kiro_crew.subagent import SubagentDelivery

    info = _finished_run("injW001")
    queued: list[str] = []
    manager = _giving_up_manager(queued)
    await _report(manager, info)
    assert len(queued) == 1 and sp.read_tombstone("injW001") is None

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    # The drain runs where the turn builds its context, as on a running gateway.
    state.context_builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.subagents = manager
    handed: list[str] = []

    async def _stream(message):  # type: ignore[no-untyped-def]
        handed.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="noted")
        yield LLMEvent(kind=EVENT_COMPLETE)

    model = MagicMock()
    model.stream = _stream
    model.stream_command = _stream
    model.context_usage_pct = MagicMock(return_value=1.0)
    state.sessions.get_or_create = AsyncMock(return_value=(model, True, False))
    slot = state.get_or_create_slot("chat-7")
    slot._titled = True
    # What the gateway's ``subagent_injection_failed`` handler records.
    slot._pending_subagent_failures.append(queued[0])
    slot.note_pending_subagent_delivery(
        queued[0], [SubagentDelivery(info.id, info.elapsed, info.credits)]
    )

    await asyncio.wait_for(_run_chat(state, slot, "what came back?"), timeout=_HANG_CEILING_SECS)
    await drain_background_tasks(state)

    assert len(handed) == 1 and "injW001" in handed[0], handed
    assert (sp.read_tombstone("injW001") or {}).get(
        "cause"
    ) == "delivered", "the parent's turn drained the notice without settling its tombstone"
    assert not slot.owes_subagent_delivery(queued)
    assert await _next_start_announcements(monkeypatch) == []


@pytest.mark.asyncio
async def test_a_delivered_report_is_still_tombstoned_by_the_run_loop(monkeypatch):
    from kiro_crew import subagent_persistence as sp

    info = _finished_run("injD001")
    await _report(_manager(), info)
    assert info._report_undelivered is False
    assert (sp.read_tombstone("injD001") or {}).get("cause") == "delivered"
    assert await _next_start_announcements(monkeypatch) == []


def _orphaned_run(agent_id: str, *, whole: bool) -> None:
    from kiro_crew import subagent_persistence as sp

    sp.create_agent_folder(agent_id, task="summarise the logs", parent_session=PARENT)
    if whole:
        assert sp.write_finished_result(agent_id, "the whole answer", sp.update_state)


def _gateway_notify(slot, transcript: list[str]):  # type: ignore[no-untyped-def]
    """``on_orphan_notify`` as the gateway wires it, over a real slot ledger."""

    async def _orphan_notify(parent_session, msg, meta=None, delivery=None):  # type: ignore[no-untyped-def]
        transcript.append(msg)
        slot._pending_subagent_failures.append(msg)
        if delivery is None:
            return True
        # Imported only on the path that takes a debt, so a manager that never
        # passes one drives this double unchanged.
        from kiro_crew.subagent import ORPHAN_NOTICE_OWED

        slot.note_pending_subagent_delivery(msg, [delivery])
        return ORPHAN_NOTICE_OWED

    return _orphan_notify


async def _boot(monkeypatch, slot, transcript):  # type: ignore[no-untyped-def]
    manager = _manager(on_orphan_notify=_gateway_notify(slot, transcript))
    dm = AsyncMock()
    monkeypatch.setattr(manager, "_send_orphan_slack_dm", dm)
    monkeypatch.setattr(manager, "_is_pid_alive", lambda _pid: False)
    await manager._reconcile_orphans()
    # A notice the slot queued with its tombstone is neither sent as a DM nor
    # tombstoned by the scan.
    dm.assert_not_awaited()
    return manager


@pytest.mark.asyncio
async def test_an_owed_notice_is_reported_as_owed_not_as_delivered(monkeypatch):
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.subagent import ORPHAN_NOTICE_OWED

    _orphaned_run("orpE001", whole=True)
    slot, transcript = _ChatSlot("chat-7"), []
    manager = _manager(on_orphan_notify=_gateway_notify(slot, transcript))
    state = {"id": "orpE001", "task": "summarise the logs", "parent_session": PARENT}
    state["result_complete"] = True
    assert await manager._notify_orphan("orpE001", state, True) == ORPHAN_NOTICE_OWED
    assert slot.owes_subagent_delivery(slot._pending_subagent_failures)


@pytest.mark.asyncio
async def test_a_restart_before_the_drain_announces_a_boot_orphan_again(monkeypatch):
    from kiro_crew import subagent_persistence as sp
    from kiro_crew.dashboard.state import _ChatSlot

    _orphaned_run("orpB001", whole=True)
    slot, transcript = _ChatSlot("chat-7"), []
    await _boot(monkeypatch, slot, transcript)
    assert len(transcript) == 1 and len(slot._pending_subagent_failures) == 1
    tomb = sp.read_tombstone("orpB001")
    # The restart: the slot and its queue end with the process.
    announced = await _next_start_announcements(monkeypatch)
    assert announced == [PARENT], (
        "a notice queued for the parent agent's next turn is lost with the restart "
        f"and not announced again (tombstone before the restart: {tomb})"
    )
    assert tomb is None


@pytest.mark.asyncio
@pytest.mark.parametrize("whole", [True, False], ids=["finished", "lost"])
async def test_a_drained_boot_orphan_notice_keeps_its_outcome_and_is_announced_once(
    monkeypatch, whole
):
    from kiro_crew import subagent_persistence as sp
    from kiro_crew.dashboard.chat_runner import settle_drained_subagent_notices
    from kiro_crew.dashboard.state import _ChatSlot

    _orphaned_run("orpC001", whole=whole)
    slot, transcript = _ChatSlot("chat-7"), []
    manager = await _boot(monkeypatch, slot, transcript)
    state = MagicMock()
    state.subagents = manager
    state._background_tasks = set()
    failures = slot._pending_subagent_failures[:]
    slot._pending_subagent_failures.clear()
    settle_drained_subagent_notices(state, slot, failures)
    await _settled(state)
    tomb = sp.read_tombstone("orpC001") or {}
    assert (tomb.get("cause"), tomb.get("recovery_action")) == ("gateway_restart", "delivered")
    assert tomb.get("outcome") == ("completed" if whole else None)
    assert await _next_start_announcements(monkeypatch) == []
    assert len(transcript) == 1


# The record a queued notice owes rides the slot ledger in memory until the drain.
# Its entry cap bounds how many records the ledger holds, not how large each is, so
# the fields the record carries from the run's ``state.json`` are bounded as they
# are built. ``last_tool`` is backend-authored (a shell tool's title is the whole
# command) and tool diagnostics persist it whole.
_HUGE_COMMAND = "echo " + "x" * 200_000


@pytest.mark.asyncio
async def test_the_record_a_queued_notice_owes_is_bounded_where_it_is_built(monkeypatch):
    from kiro_crew import subagent_persistence as sp
    from kiro_crew.dashboard.state import _ChatSlot

    _orphaned_run("orpL001", whole=False)
    assert sp.update_state("orpL001", last_tool=_HUGE_COMMAND, turns=3)
    slot, transcript = _ChatSlot("chat-7"), []
    await _boot(monkeypatch, slot, transcript)
    (delivery,) = slot.take_pending_subagent_deliveries(slot._pending_subagent_failures)
    record = delivery.tombstone
    assert record is not None
    assert (
        len(record["last_tool"]) <= 80
    ), f"the ledger retained {len(record['last_tool'])} characters"
    assert record["last_tool"].startswith("echo xxx")
    assert (record["cause"], record["recovery_action"], record["turns"]) == (
        "gateway_restart",
        "delivered",
        3,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [True, False], ids=["injected", "digest"])
async def test_a_tombstone_the_scan_writes_carries_the_bounded_title(monkeypatch, injected):
    from kiro_crew import subagent_persistence as sp

    _orphaned_run("orpL002", whole=False)
    assert sp.update_state("orpL002", last_tool=_HUGE_COMMAND, turns=5)
    manager = _manager()

    async def _inject(parent_session, msg, meta=None, delivery=None):  # type: ignore[no-untyped-def]
        return injected

    monkeypatch.setattr(manager, "_try_inject_orphan_notification", _inject)
    monkeypatch.setattr(manager, "_send_orphan_slack_dm", AsyncMock())
    monkeypatch.setattr(manager, "_is_pid_alive", lambda _pid: False)
    await manager._reconcile_orphans()
    tomb = sp.read_tombstone("orpL002") or {}
    assert tomb.get("cause") == "gateway_restart"
    assert tomb.get("turns") == 5
    assert 0 < len(tomb.get("last_tool", "")) <= 80, tomb.get("last_tool", "")[:120]


@pytest.mark.parametrize(
    ("stored", "kept"),
    [
        (
            {"pid": 4242, "turns": 7, "last_tool": "ls\n   -la"},
            {"pid": 4242, "turns": 7, "last_tool": "ls -la"},
        ),
        (
            {"pid": "4242", "turns": "7", "last_tool": ["ls"]},
            {"pid": None, "turns": 0, "last_tool": ""},
        ),
        (
            {"pid": True, "turns": True, "last_tool": 5},
            {"pid": None, "turns": 0, "last_tool": ""},
        ),
        ({}, {"pid": None, "turns": 0, "last_tool": ""}),
    ],
    ids=["as-stored", "strings-and-lists", "booleans-and-numbers", "absent"],
)
def test_the_carried_fields_keep_only_the_types_the_run_stores(stored, kept):
    from kiro_crew.subagent_manager.monitoring import orphan_carried_fields

    assert orphan_carried_fields(stored) == kept

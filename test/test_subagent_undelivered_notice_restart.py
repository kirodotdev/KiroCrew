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


def _finished_run(agent_id: str, parent: str = PARENT):  # type: ignore[no-untyped-def]
    from kiro_crew import subagent_persistence as sp
    from kiro_crew.subagent import SubagentInfo

    sp.create_agent_folder(agent_id, task="summarise the logs", parent_session=parent)
    assert sp.write_finished_result(agent_id, "the whole answer", sp.update_state)
    info = SubagentInfo(id=agent_id, task="summarise the logs")
    info.parent_session_key = parent
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
        # slack/gateway_runtime/subagent_wiring.py ``_subagent_done``: every attempt failed, so it announces the
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


async def _parent_with_a_queued_notice(monkeypatch, tmp_path, agent_id: str, stream):  # type: ignore[no-untyped-def]
    """A real ``DashboardState`` whose chat slot holds *agent_id*'s owed notice.

    Only the model is a stand-in: *stream* is what it answers each prompt with.
    Returns the state, the slot and the queued notice texts.
    """
    from chat_test_helpers import _make_state

    from kiro_crew import subagent_persistence as sp
    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader
    from kiro_crew.subagent import SubagentDelivery

    info = _finished_run(agent_id)
    queued: list[str] = []
    manager = _giving_up_manager(queued)
    await _report(manager, info)
    assert len(queued) == 1 and sp.read_tombstone(agent_id) is None

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
    model = MagicMock()
    model.stream = stream
    model.stream_command = stream
    model.context_usage_pct = MagicMock(return_value=1.0)
    state.sessions.get_or_create = AsyncMock(return_value=(model, True, False))
    slot = state.get_or_create_slot("chat-7")
    slot._titled = True
    # What the gateway's ``subagent_injection_failed`` handler records.
    slot._pending_subagent_failures.append(queued[0])
    slot.note_pending_subagent_delivery(
        queued[0], [SubagentDelivery(info.id, info.elapsed, info.credits)]
    )
    return state, slot, queued


@pytest.mark.asyncio
async def test_the_parent_turn_settles_the_notice_it_drains(monkeypatch, tmp_path):
    """The drain is wired into the parent's next turn.

    ``_run_chat`` runs for real on a ``DashboardState``; only the model is a
    stand-in, which records the message it is handed. The turn hands the queued
    notice to the model and settles the run's tombstone, so the next start does
    not announce the run again.
    """
    from chat_test_helpers import drain_background_tasks

    from kiro_crew import subagent_persistence as sp
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    handed: list[str] = []

    async def _stream(message):  # type: ignore[no-untyped-def]
        handed.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="noted")
        yield LLMEvent(kind=EVENT_COMPLETE)

    state, slot, queued = await _parent_with_a_queued_notice(
        monkeypatch, tmp_path, "injW001", _stream
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
@pytest.mark.parametrize("ending", ["error", "process_died", "empty"])
async def test_a_turn_that_ends_before_the_model_reads_the_notice_leaves_it_owed(
    monkeypatch, tmp_path, ending
):
    """The notice is settled by the prompt the model consumes, never by an earlier one.

    The first prompt fails before the model sends any event (a plain error ends the
    turn on an error card and the user asks again; a process death replays the same
    prompt by itself), or the model's first answer is empty, which takes its
    consumption back and replays the prompt. Each time the first turn settles nothing
    and puts the notice back still owed, so a restart at that point would announce
    the run. The next prompt carries the notice exactly once and its consumption
    settles it.
    """
    from chat_test_helpers import drain_background_tasks

    from kiro_crew import subagent_persistence as sp
    from kiro_crew.acp.client import AcpProcessDied
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    handed: list[str] = []
    debt_at_second_prompt: list[tuple[bool, object]] = []

    async def _stream(message):  # type: ignore[no-untyped-def]
        handed.append(message)
        if len(handed) == 1:
            if ending != "empty":
                raise (AcpProcessDied if ending == "process_died" else RuntimeError)("died")
        else:
            debt_at_second_prompt.append(
                (slot.owes_subagent_delivery(queued), sp.read_tombstone("injF001"))
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="noted")
        yield LLMEvent(kind=EVENT_COMPLETE)

    state, slot, queued = await _parent_with_a_queued_notice(
        monkeypatch, tmp_path, "injF001", _stream
    )
    # The error arm records the failure on the session manager.
    state.sessions.record_failure = AsyncMock()

    await asyncio.wait_for(_run_chat(state, slot, "what came back?"), timeout=_HANG_CEILING_SECS)
    if ending == "error":
        await asyncio.wait_for(_run_chat(state, slot, "again?"), timeout=_HANG_CEILING_SECS)
    elif slot.task is not None:
        await asyncio.wait_for(slot.task, timeout=_HANG_CEILING_SECS)
    await drain_background_tasks(state)

    assert len(handed) == 2, handed
    assert debt_at_second_prompt == [(True, None)], "the first turn settled an unread notice"
    assert handed[1].count(queued[0]) == 1, handed[1]
    assert slot._pending_subagent_failures == []
    assert (sp.read_tombstone("injF001") or {}).get("cause") == "delivered"
    assert not slot.owes_subagent_delivery(queued)
    assert await _next_start_announcements(monkeypatch) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lands", [True, False], ids=["landed", "cut-off"])
async def test_a_turn_started_in_the_channel_thread_takes_the_notice_its_tab_queued(
    monkeypatch, tmp_path, lands
):
    """A channel conversation's tab queues the notice; its next turn runs in the thread.

    ``ChannelTurns`` (the pipeline Discord, Teams and the other channels share) runs
    on a real ``ContextBuilder`` that a real ``DashboardState`` wired; only the model
    is a stand-in. A turn that lands hands the model the notice once and settles the
    run's tombstone; one cut off before its completion puts the notice back on the
    tab's queue, still owed, so neither the agent nor the next start loses it.
    """
    from channel_turn_fakes import FakeSessions, RecordingRenderer, ScriptedProvider, answer, text
    from channel_turns_helpers import KEY, _asker, _turns
    from chat_test_helpers import _make_state, drain_background_tasks

    from kiro_crew import session_surface
    from kiro_crew import subagent_persistence as sp
    from kiro_crew.context import ContextBuilder
    from kiro_crew.dashboard.chat_utils_slots import _sync_dashboard_slots, dashboard_slot_key
    from kiro_crew.memory import MemoryStore
    from kiro_crew.messaging import dispatch as D
    from kiro_crew.skills import SkillsLoader
    from kiro_crew.subagent import SubagentDelivery

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(D, "channel_inbound_permitted", AsyncMock(return_value=True))
    monkeypatch.setattr(D, "publish_turn_identity", AsyncMock())
    # The registry the dashboard publishes its tabs to, restored at teardown.
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset())
    monkeypatch.setattr(session_surface, "_dashboard_tabs", frozenset())
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    # The conversation's tab: a channel-born slot runs on the channel's own key.
    slot = state.get_or_create_slot(KEY.replace(":", "_"))
    slot.linked_session_key = KEY
    _sync_dashboard_slots(state)
    assert dashboard_slot_key(KEY) == slot.key

    info = _finished_run("injT001", parent=KEY)
    queued: list[str] = []
    manager = _giving_up_manager(queued)
    state.subagents = manager
    await _report(manager, info)
    assert len(queued) == 1 and sp.read_tombstone("injT001") is None
    # What the gateway's ``subagent_injection_failed`` handler records.
    slot._pending_subagent_failures.append(queued[0])
    slot.note_pending_subagent_delivery(
        queued[0], [SubagentDelivery(info.id, info.elapsed, info.credits)]
    )
    provider = ScriptedProvider(answer("noted") if lands else [text("cut")])

    await asyncio.wait_for(
        _turns(FakeSessions(provider), ctx=builder).answer(
            _asker(), "what came back?", RecordingRenderer()
        ),
        timeout=_HANG_CEILING_SECS,
    )
    await drain_background_tasks(state)

    # The notice's own closing line, which nothing else in the prompt carries.
    assert len(provider.prompts) == 1
    assert provider.prompts[0].count(queued[0].splitlines()[-1]) == 1, provider.prompts[0]
    if lands:
        assert (sp.read_tombstone("injT001") or {}).get("cause") == "delivered"
        assert slot._pending_subagent_failures == [] and not slot.owes_subagent_delivery(queued)
        assert await _next_start_announcements(monkeypatch) == []
    else:
        assert sp.read_tombstone("injT001") is None
        assert slot._pending_subagent_failures == queued and slot.owes_subagent_delivery(queued)


def test_a_channel_build_keeps_the_tab_notices_outside_the_typed_text(tmp_path):
    """The notices go ahead of the typed text, and only the engine that read them settles.

    The reported user span still bounds what was typed, and a settle without
    ``tab_notices`` (a turn in the tab, which drains its queue itself) leaves the
    channel turn's notices to that turn's own settle.
    """
    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory import MemoryStore
    from kiro_crew.messaging.turn_bracket import rollback_skill_bodies
    from kiro_crew.skills import SkillsLoader

    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    settled: list[bool] = []
    builder.tab_notices_for_session = lambda key: (
        ["run r1 finished, result on disk"],
        settled.append,
    )
    span: list[int] = []
    owned: list = []

    full, _ = builder.build_message(
        "hi there",
        False,
        "weixin:agentA:direct:userA",
        user_text_range=(0, 8),
        user_span_out=span,
        tab_notices=owned,
    )

    assert full.count("run r1 finished, result on disk") == 1
    assert full[span[0] : span[1]] == "hi there"
    rollback_skill_bodies(builder, "weixin:agentA:direct:userA", landed=True)
    assert settled == []
    rollback_skill_bodies(builder, "weixin:agentA:direct:userA", landed=True, tab_notices=owned)
    assert settled == [True] and owned == []


def test_a_turn_settles_only_the_tab_notices_its_own_build_read(tmp_path):
    """Another turn's exit on the same session never settles this turn's notices.

    One turn exits without building (a hook reply), and another built before this
    one and settles after it built (a predecessor torn down by a reset): neither
    takes the settle this turn's build holds.
    """
    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory import MemoryStore
    from kiro_crew.messaging.turn_bracket import TurnBracket
    from kiro_crew.skills import SkillsLoader

    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    settled: list[tuple[int, bool]] = []
    builds = iter(range(2))

    def _read(key):  # type: ignore[no-untyped-def]
        build = next(builds)
        return ["run r1 finished"], lambda landed: settled.append((build, landed))

    builder.tab_notices_for_session = _read
    key = "weixin:agentA:direct:userA"
    predecessor = TurnBracket(MagicMock(), builder, key)
    builder.build_message("one", False, key, tab_notices=predecessor.tab_notices)
    turn = TurnBracket(MagicMock(), builder, key)
    builder.build_message("two", False, key, tab_notices=turn.tab_notices)

    TurnBracket(MagicMock(), builder, key).settle()
    predecessor.settle()
    assert settled == [(0, False)]
    turn.landed(True)
    turn.settle()
    assert settled == [(0, False), (1, True)]


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
